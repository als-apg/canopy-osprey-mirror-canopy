"""PVBroker: on-demand monitors, diff fan-out, refcounted teardown.

Driven deterministically via FakeConnector (emit on demand) + FakeWebSocket,
without starting the background loops — tests call `_drain_nowait` / `_flush`.
"""

import asyncio
import contextlib

import pytest

from canopy.epics.base import PVWriteFailed
from canopy.hare.config import HareConfig
from canopy.contracts.sample import AlarmSeverity
from canopy.epics.pv_protocol import InvalidPVName
from tests.canopy.hare.conftest import FakeWebSocket
from canopy.hare.services.broker import PVBroker


class TestResourceCaps:
    def test_event_queue_is_bounded(self, fake_connector):
        broker = PVBroker(fake_connector, HareConfig(event_queue_maxsize=7))
        assert broker._queue.maxsize == 7

    async def test_subscribe_caps_pvs_per_message(self, fake_connector):
        broker = PVBroker(fake_connector, HareConfig(max_pvs_per_message=3))
        broker.add_client("c1", FakeWebSocket())
        await broker.subscribe("c1", [f"PV:{i}" for i in range(10)])
        # Only the first 3 reach the connector as monitors.
        assert len(fake_connector.callbacks) == 3

    async def test_subscribe_caps_total_per_client(self, fake_connector):
        broker = PVBroker(fake_connector, HareConfig(max_subscriptions_per_client=4))
        broker.add_client("c1", FakeWebSocket())
        await broker.subscribe("c1", ["PV:0", "PV:1", "PV:2"])
        await broker.subscribe("c1", ["PV:3", "PV:4", "PV:5"])  # only PV:3 fits
        assert len(broker.registry.client_pvs("c1")) == 4

    async def test_resubscribe_is_idempotent_under_cap(self, fake_connector):
        broker = PVBroker(fake_connector, HareConfig(max_subscriptions_per_client=2))
        broker.add_client("c1", FakeWebSocket())
        await broker.subscribe("c1", ["PV:A", "PV:B"])
        # Re-subscribing existing PVs must not be rejected by the cap.
        await broker.subscribe("c1", ["PV:A", "PV:B"])
        assert broker.registry.client_pvs("c1") == {"PV:A", "PV:B"}


async def test_subscribe_starts_monitor_and_sends_initial(broker, fake_connector):
    ws = FakeWebSocket()
    broker.add_client("c1", ws)
    await broker.subscribe("c1", ["PV:A"])

    assert "PV:A" in fake_connector.callbacks  # monitor started
    assert ws.sent and ws.sent[0]["type"] == "initial"
    assert "PV:A" in ws.sent[0]["data"]


async def test_subscribe_skips_invalid_names(broker, fake_connector):
    # A malformed name in the batch (bare prefix, empty) must be dropped, not
    # reach the connector, and must not stop the valid PVs from subscribing.
    ws = FakeWebSocket()
    broker.add_client("c1", ws)
    await broker.subscribe("c1", ["PV:A", "pva://", "", "PVA://"])

    assert "PV:A" in fake_connector.callbacks
    assert "pva://" not in fake_connector.callbacks
    assert "" not in fake_connector.callbacks
    assert "PVA://" not in fake_connector.callbacks


async def test_subscribe_notifies_client_of_rejected_names(broker, fake_connector):
    # Rejected names must be surfaced to the client so it can tell them apart
    # from valid PVs that simply haven't produced a value yet.
    ws = FakeWebSocket()
    broker.add_client("c1", ws)
    await broker.subscribe("c1", ["PV:A", "pva://", ""])

    errors = [m for m in ws.sent if m["type"] == "error"]
    assert errors
    err = errors[0]
    assert err["code"] == "invalid_pv_name"
    assert set(err["pvNames"]) == {"pva://", ""}


async def test_subscribe_all_valid_sends_no_error(broker, fake_connector):
    ws = FakeWebSocket()
    broker.add_client("c1", ws)
    await broker.subscribe("c1", ["PV:A", "PV:B"])

    assert not any(m["type"] == "error" for m in ws.sent)


async def test_get_value_rejects_invalid_name(broker):
    with pytest.raises(InvalidPVName):
        await broker.get_value("pva://")


async def test_put_rejects_invalid_name(broker):
    with pytest.raises(InvalidPVName):
        await broker.put("", 1.0)


async def test_event_flushed_as_diff(broker, fake_connector):
    ws = FakeWebSocket()
    broker.add_client("c1", ws)
    await broker.subscribe("c1", ["PV:A"])
    ws.sent.clear()

    fake_connector.emit("PV:A", 1.23, severity=AlarmSeverity.MINOR, ts=99)
    await broker._drain_nowait()
    await broker._flush()

    diffs = [m for m in ws.sent if m["type"] == "diff"]
    assert diffs
    payload = diffs[-1]["data"]["PV:A"]
    assert payload["value"] == 1.23
    assert payload["severity"] == "MINOR"
    assert payload["tsNs"] == 99  # camelCase on the wire


async def test_diff_only_goes_to_subscribers(broker, fake_connector):
    ws1, ws2 = FakeWebSocket(), FakeWebSocket()
    broker.add_client("c1", ws1)
    broker.add_client("c2", ws2)
    await broker.subscribe("c1", ["PV:A"])
    await broker.subscribe("c2", ["PV:B"])
    ws1.sent.clear()
    ws2.sent.clear()

    fake_connector.emit("PV:A", 5.0)
    await broker._drain_nowait()
    await broker._flush()

    assert any(m["type"] == "diff" for m in ws1.sent)
    assert not any(m["type"] == "diff" for m in ws2.sent)


async def test_refcount_keeps_monitor_until_last_subscriber_leaves(broker, fake_connector):
    ws1, ws2 = FakeWebSocket(), FakeWebSocket()
    broker.add_client("c1", ws1)
    broker.add_client("c2", ws2)
    await broker.subscribe("c1", ["PV:A"])
    await broker.subscribe("c2", ["PV:A"])

    await broker.unsubscribe("c1", ["PV:A"])
    assert "PV:A" in fake_connector.callbacks  # c2 still subscribed

    await broker.unsubscribe("c2", ["PV:A"])
    assert "PV:A" not in fake_connector.callbacks  # monitor closed


async def test_remove_client_closes_its_monitors(broker, fake_connector):
    ws = FakeWebSocket()
    broker.add_client("c1", ws)
    await broker.subscribe("c1", ["PV:A"])
    await broker.remove_client("c1")
    assert "PV:A" not in fake_connector.callbacks


async def test_get_value_falls_back_to_one_shot(broker, fake_connector):
    # Not subscribed -> broker issues a one-shot connector.get
    value = await broker.get_value("PV:Z")
    assert value.pv == "PV:Z"
    assert value.connected is True


async def test_initial_frame_includes_metadata(broker, fake_connector):
    ws = FakeWebSocket()
    broker.add_client("c1", ws)
    await broker.subscribe("c1", ["PV:A"])

    initial = ws.sent[0]
    assert initial["type"] == "initial"
    assert "meta" in initial
    meta = initial["meta"]["PV:A"]
    assert meta["valueKind"] == "scalar"  # camelCase on the wire
    assert meta["units"] == "mm"


async def test_diff_frame_carries_no_metadata(broker, fake_connector):
    ws = FakeWebSocket()
    broker.add_client("c1", ws)
    await broker.subscribe("c1", ["PV:A"])
    ws.sent.clear()

    fake_connector.emit("PV:A", 1.0)
    await broker._drain_nowait()
    await broker._flush()

    diff = [m for m in ws.sent if m["type"] == "diff"][-1]
    assert "meta" not in diff
    assert diff["data"]["PV:A"]["valueKind"] == "scalar"


async def test_get_describe_falls_back_to_one_shot(broker):
    meta = await broker.get_describe("PV:Q")
    assert meta.pv == "PV:Q"
    assert meta.value_kind.value == "scalar"


async def test_put_delegates_to_connector(broker, fake_connector):
    await broker.put("PV:A", 42.0)
    assert fake_connector.puts == [("PV:A", 42.0, True, None)]


async def test_put_passes_wait_and_timeout(broker, fake_connector):
    await broker.put("PV:A", 1, wait=False, timeout=0.5)
    assert fake_connector.puts == [("PV:A", 1, False, 0.5)]


async def test_put_propagates_connector_failure(broker, fake_connector):
    fake_connector.put_error = PVWriteFailed("boom")
    with pytest.raises(PVWriteFailed):
        await broker.put("PV:A", 1.0)


async def test_put_invalidates_cache_so_read_is_fresh(broker, fake_connector):
    # A monitored PV's value is cached; after a put, get_value must not serve the
    # stale cache -- it re-reads through the connector so put-then-read is consistent.
    broker.add_client("c1", FakeWebSocket())
    await broker.subscribe("c1", ["PV:A"])
    fake_connector.emit("PV:A", 1.0)
    await broker._drain_nowait()
    assert (await broker.get_value("PV:A")).value == 1.0  # served from cache

    await broker.put("PV:A", 9.9)
    assert "PV:A" not in broker._latest  # cache invalidated
    assert (await broker.get_value("PV:A")).value == 0.0  # fresh one-shot connector.get


class TestLoopSupervision:
    async def test_flush_loop_survives_iteration_error(self, broker, monkeypatch):
        # A single failing flush must not kill the loop: fan-out has to keep
        # working for every client after a transient error.
        broker.add_client("c1", FakeWebSocket())
        await broker.subscribe("c1", ["PV:A"])

        calls = {"n": 0}
        real_flush = broker._flush

        async def flaky_flush():
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("transient flush error")
            await real_flush()

        monkeypatch.setattr(broker, "_flush", flaky_flush)
        broker._running = True
        task = asyncio.create_task(broker._flush_loop())
        try:
            await asyncio.sleep(0.05)  # several 10ms intervals
            assert not task.done()  # survived the first-iteration error
            assert calls["n"] >= 2  # and kept flushing afterwards
        finally:
            broker._running = False
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def test_supervisor_restarts_crashed_loop_when_running(self, broker):
        broker._running = True

        async def crash():
            raise RuntimeError("scaffolding boom")

        dead = asyncio.create_task(crash(), name="heartbeat")
        with contextlib.suppress(BaseException):
            await dead
        broker._tasks = [dead]

        broker._supervise_loop(dead)  # simulate the done-callback firing

        assert len(broker._tasks) == 1
        replacement = broker._tasks[0]
        assert replacement is not dead
        assert not replacement.done()

        broker._running = False
        replacement.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await replacement

    async def test_supervisor_does_not_restart_when_stopped(self, broker):
        broker._running = False

        async def crash():
            raise RuntimeError("boom")

        dead = asyncio.create_task(crash(), name="flush")
        with contextlib.suppress(BaseException):
            await dead
        broker._tasks = [dead]

        broker._supervise_loop(dead)

        assert broker._tasks == [dead]  # no restart during shutdown


def _record_calls(connector, method: str, *, failures: int = 0) -> list[str]:
    """Wrap a connector coroutine method so every call is recorded and the first few raise.

    Parameters
    ----------
    connector : FakeConnector
        Connector whose method is replaced on the instance.
    method : str
        Name of the coroutine method to wrap, such as ``"get"`` or ``"describe"``.
    failures : int, optional
        Number of leading calls that raise `TimeoutError`; later calls delegate
        to the original method.

    Returns
    -------
    list[str]
        PV names passed to the wrapped method, one entry per call in call order.
    """
    original = getattr(connector, method)
    calls: list[str] = []

    async def wrapped(pv, **kwargs):
        """Record the call, then raise or delegate to the original method.

        Parameters
        ----------
        pv : str
            PV name the broker passed.
        **kwargs
            Keyword arguments forwarded to the original method.

        Returns
        -------
        object
            The original method's result.

        Raises
        ------
        TimeoutError
            For each of the first ``failures`` calls.
        """
        calls.append(pv)
        if len(calls) <= failures:
            raise TimeoutError(f"{method} not ready")
        return await original(pv, **kwargs)

    setattr(connector, method, wrapped)
    return calls


def _initial_frames(ws: FakeWebSocket) -> list[dict]:
    """Collect the ``initial`` frames a client received.

    Parameters
    ----------
    ws : FakeWebSocket
        Client channel whose sent frames are inspected.

    Returns
    -------
    list[dict]
        Every frame of type ``initial``, in send order.
    """
    return [m for m in ws.sent if m["type"] == "initial"]


async def test_failed_seed_is_retried_for_late_subscribers(fake_connector):
    """A later subscribe of an already-monitored PV retries a seed whose ``get`` failed.

    With a zero backoff window, the late subscriber receives the PV's value in
    its ``initial`` frame once the connector recovers, even though the monitor
    never ticked.
    """
    broker = PVBroker(fake_connector, HareConfig(seed_retry_backoff_s=0))
    _record_calls(fake_connector, "get", failures=1)

    first = FakeWebSocket()
    broker.add_client("c1", first)
    await broker.subscribe("c1", ["PV:SEED"])
    initial_frames = _initial_frames(first)
    assert not initial_frames or "PV:SEED" not in initial_frames[0].get("data", {})

    late = FakeWebSocket()
    broker.add_client("c2", late)
    await broker.subscribe("c2", ["PV:SEED"])
    late_initials = _initial_frames(late)
    assert late_initials and "PV:SEED" in late_initials[0]["data"]


async def test_failed_seed_is_not_retried_within_backoff(fake_connector):
    """A subscribe inside the backoff window does not call ``get`` again for a PV whose seed failed.

    The late subscriber still receives the metadata that did seed, but no value.
    """
    broker = PVBroker(fake_connector, HareConfig(seed_retry_backoff_s=3600))
    gets = _record_calls(fake_connector, "get", failures=1)

    broker.add_client("c1", FakeWebSocket())
    await broker.subscribe("c1", ["PV:SEED"])

    late = FakeWebSocket()
    broker.add_client("c2", late)
    await broker.subscribe("c2", ["PV:SEED"])

    assert gets == ["PV:SEED"]
    late_initials = _initial_frames(late)
    assert late_initials
    assert "PV:SEED" not in late_initials[0]["data"]
    assert "PV:SEED" in late_initials[0]["meta"]


async def test_failed_seed_is_retried_after_backoff_and_success_clears_deadline(fake_connector):
    """A failed seed sets a retry deadline; a retry after the window that succeeds clears it."""
    broker = PVBroker(fake_connector, HareConfig(seed_retry_backoff_s=0))
    gets = _record_calls(fake_connector, "get", failures=1)

    broker.add_client("c1", FakeWebSocket())
    await broker.subscribe("c1", ["PV:SEED"])
    assert "PV:SEED" in broker._seed_retry_after

    broker.add_client("c2", FakeWebSocket())
    await broker.subscribe("c2", ["PV:SEED"])

    assert gets == ["PV:SEED", "PV:SEED"]
    assert "PV:SEED" in broker._latest
    assert "PV:SEED" not in broker._seed_retry_after


@pytest.mark.parametrize(
    ("backoff_s", "expected_describes"),
    [pytest.param(3600, 1, id="within-backoff"), pytest.param(0, 2, id="after-backoff")],
)
async def test_describe_failure_retries_only_describe_after_backoff(fake_connector, backoff_s, expected_describes):
    """A PV whose value seeded but whose ``describe`` failed re-runs only ``describe``, and only after the window.

    The cached value is never fetched again; ``describe`` is retried by a later
    subscribe once the backoff has elapsed and skipped while it has not.
    """
    broker = PVBroker(fake_connector, HareConfig(seed_retry_backoff_s=backoff_s))
    gets = _record_calls(fake_connector, "get")
    describes = _record_calls(fake_connector, "describe", failures=1)

    broker.add_client("c1", FakeWebSocket())
    await broker.subscribe("c1", ["PV:META"])
    broker.add_client("c2", FakeWebSocket())
    await broker.subscribe("c2", ["PV:META"])

    assert gets == ["PV:META"]
    assert describes == ["PV:META"] * expected_describes


async def test_stop_monitors_clears_seed_retry_deadline(fake_connector):
    """Releasing a PV drops its seed retry deadline and seed lock, so re-subscribing seeds it at once despite the window."""
    broker = PVBroker(fake_connector, HareConfig(seed_retry_backoff_s=3600))
    gets = _record_calls(fake_connector, "get", failures=1)

    broker.add_client("c1", FakeWebSocket())
    await broker.subscribe("c1", ["PV:SEED"])
    assert "PV:SEED" in broker._seed_retry_after
    assert "PV:SEED" in broker._seed_locks

    await broker.unsubscribe("c1", ["PV:SEED"])
    assert "PV:SEED" not in broker._seed_retry_after
    assert "PV:SEED" not in broker._seed_locks

    again = FakeWebSocket()
    broker.add_client("c2", again)
    await broker.subscribe("c2", ["PV:SEED"])

    assert gets == ["PV:SEED", "PV:SEED"]
    assert "PV:SEED" in _initial_frames(again)[0]["data"]


@pytest.mark.parametrize("get_fails", [pytest.param(True, id="unreachable"), pytest.param(False, id="slow")])
async def test_concurrent_subscribes_share_one_seed_attempt(fake_connector, get_fails):
    """Concurrent subscribes of an unseeded PV make one ``get`` and one ``describe`` between them.

    The first subscriber's ``get`` stalls until released, so the second
    subscribe reaches the seed step while that attempt is still in flight. It
    waits for the attempt instead of starting its own, then sees the value it
    produced or, when it failed, the retry deadline it set.
    """
    broker = PVBroker(fake_connector, HareConfig(seed_retry_backoff_s=3600))
    original_get = fake_connector.get
    release = asyncio.Event()
    gets: list[str] = []

    async def stalled_get(pv, **kwargs):
        gets.append(pv)
        await release.wait()
        if get_fails:
            raise TimeoutError("get timed out")
        return await original_get(pv, **kwargs)

    fake_connector.get = stalled_get
    describes = _record_calls(fake_connector, "describe")

    first, second = FakeWebSocket(), FakeWebSocket()
    broker.add_client("c1", first)
    broker.add_client("c2", second)
    subscribes = [
        asyncio.create_task(broker.subscribe("c1", ["PV:SEED"])),
        asyncio.create_task(broker.subscribe("c2", ["PV:SEED"])),
    ]
    await asyncio.sleep(0.05)
    release.set()
    await asyncio.wait_for(asyncio.gather(*subscribes), 1.0)

    assert gets == ["PV:SEED"]
    assert describes == ["PV:SEED"]
    for ws in (first, second):
        (initial,) = _initial_frames(ws)
        assert ("PV:SEED" in initial["data"]) is not get_fails
        assert "PV:SEED" in initial["meta"]
