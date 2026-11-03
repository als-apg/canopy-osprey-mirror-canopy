"""PVBroker: on-demand monitors, diff fan-out, refcounted teardown.

Driven deterministically via FakeConnector (emit on demand) + FakeWebSocket,
without starting the background loops — tests call `_drain_nowait` / `_flush`,
and `settle` to ingest the monitor's first update and wait for the describe it
starts.
"""

import asyncio
import contextlib

import pytest

from tests.conftest import wait_for
from canopy.epics.base import PVWriteFailed
from canopy.hare.config import HareConfig
from canopy.contracts.pv import PVMeta, PVValue
from canopy.contracts.sample import AlarmSeverity
from canopy.epics.pv_protocol import InvalidPVName
from tests.canopy.hare.conftest import FakeWebSocket, settle
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
    await settle(broker)

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
    await settle(broker)
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
    await settle(broker)
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
    await settle(broker)

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
    await settle(broker)
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
    await settle(broker)
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
            assert await wait_for(lambda: calls["n"] >= 2)  # kept flushing after the error
            assert not task.done()  # survived the first-iteration error
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


def _gate_describe(connector, gated: set[str]) -> tuple[asyncio.Event, list[str]]:
    """Make ``connector.describe`` hold the given PVs until the returned gate opens, recording every call.

    Parameters
    ----------
    connector : FakeConnector
        Connector whose ``describe`` is replaced on the instance.
    gated : set[str]
        PV names whose describes block on the gate; others answer at once.

    Returns
    -------
    tuple[asyncio.Event, list[str]]
        The gate to open, and the PV names passed to ``describe`` in call order.
    """
    original = connector.describe
    gate = asyncio.Event()
    calls: list[str] = []

    async def gated_describe(pv):
        calls.append(pv)
        if pv in gated:
            await gate.wait()
        return await original(pv)

    connector.describe = gated_describe
    return gate, calls


class _GatedWebSocket(FakeWebSocket):
    """A client whose first send blocks until ``release`` is set, so a fan-out can be caught mid-send."""

    def __init__(self) -> None:
        super().__init__()
        self.release = asyncio.Event()
        self.blocked = False

    async def send_json(self, data) -> None:
        if not self.blocked:
            self.blocked = True
            await self.release.wait()
        await super().send_json(data)


def _initials_with_meta(ws: FakeWebSocket, pv: str) -> list[dict]:
    """Return the ``initial`` frames a client received that carry metadata for ``pv``."""
    return [m for m in _initial_frames(ws) if pv in m["meta"]]


def _uncancellable(gate: asyncio.Event):
    """Wait on ``gate`` even when cancelled, like a connector call that cannot be aborted mid-flight."""

    async def wait() -> None:
        try:
            await gate.wait()
        except asyncio.CancelledError:
            await gate.wait()

    return wait


async def test_subscribe_returns_before_the_describe_answers(fake_connector):
    """``subscribe`` returns while the PV's describe is pending; value and metadata arrive later in one ``initial``."""
    broker = PVBroker(fake_connector, HareConfig())
    gate, describes = _gate_describe(fake_connector, {"PV:SLOW"})
    ws = FakeWebSocket()
    broker.add_client("c1", ws)

    await asyncio.wait_for(broker.subscribe("c1", ["PV:SLOW"]), 0.5)
    await broker._drain_nowait()
    await asyncio.sleep(0)
    assert describes == ["PV:SLOW"]
    assert not _initial_frames(ws)

    gate.set()
    await broker.drain_seeds()
    (initial,) = _initial_frames(ws)
    assert "PV:SLOW" in initial["data"]
    assert "PV:SLOW" in initial["meta"]


async def test_dead_pv_in_a_batch_does_not_delay_the_live_pvs(fake_connector):
    """A live PV's value and metadata arrive while a dead PV in the same batch is still waiting on its channel.

    The dead PV follows in its own frame as ``connected: false`` without
    metadata once its connect timer fires, and is never described.
    """
    broker = PVBroker(fake_connector, HareConfig(seed_timeout_s=0.5))
    fake_connector.dead.add("PV:DEAD")
    describes = _record_calls(fake_connector, "describe")
    ws = FakeWebSocket()
    broker.add_client("c1", ws)

    await broker.subscribe("c1", ["PV:LIVE", "PV:DEAD"])
    await broker._drain_nowait()
    assert await wait_for(lambda: _initials_with_meta(ws, "PV:LIVE"), timeout=0.2)
    assert not any("PV:DEAD" in m["data"] for m in _initial_frames(ws))

    await broker.drain_seeds()
    (dead_frame,) = [m for m in _initial_frames(ws) if "PV:DEAD" in m["data"]]
    assert dead_frame["data"]["PV:DEAD"]["connected"] is False
    assert "PV:DEAD" not in dead_frame["meta"]
    assert describes == ["PV:LIVE"]


async def test_dead_pv_in_an_earlier_subscribe_does_not_delay_a_later_one(fake_connector):
    """A client's later subscribe is answered while a dead PV from an earlier one is still waiting on its channel."""
    broker = PVBroker(fake_connector, HareConfig(seed_timeout_s=0.5))
    fake_connector.dead.add("PV:DEAD")
    ws = FakeWebSocket()
    broker.add_client("c1", ws)

    await broker.subscribe("c1", ["PV:DEAD"])
    await asyncio.wait_for(broker.subscribe("c1", ["PV:LIVE"]), 0.5)
    await broker._drain_nowait()
    assert await wait_for(lambda: _initials_with_meta(ws, "PV:LIVE"), timeout=0.2)

    await broker.drain_seeds()


async def test_more_dead_pvs_than_slots_do_not_delay_a_live_pv_in_the_batch(fake_connector):
    """With more dead PVs than ``seed_concurrency`` ahead of a live one, the live PV's metadata still arrives at once.

    Dead channels wait on a timer, not on a seed slot, so only the live PV's
    describe takes one.
    """
    broker = PVBroker(fake_connector, HareConfig(seed_concurrency=1, seed_timeout_s=0.5))
    dead = [f"PV:DEAD{i}" for i in range(4)]
    fake_connector.dead.update(dead)
    describes = _record_calls(fake_connector, "describe")
    ws = FakeWebSocket()
    broker.add_client("c1", ws)

    await broker.subscribe("c1", [*dead, "PV:LIVE"])
    await broker._drain_nowait()
    assert await wait_for(lambda: _initials_with_meta(ws, "PV:LIVE"), timeout=0.2)
    assert describes == ["PV:LIVE"]

    await broker.drain_seeds()
    assert all(broker._latest[pv].connected is False for pv in dead)
    assert describes == ["PV:LIVE"]


async def test_dead_pvs_on_one_client_do_not_delay_another_clients_metadata(fake_connector):
    """Another client's live PV is described at once while a first client's dead PVs outnumber the seed slots."""
    broker = PVBroker(fake_connector, HareConfig(seed_concurrency=1, seed_timeout_s=0.5))
    dead = [f"PV:DEAD{i}" for i in range(4)]
    fake_connector.dead.update(dead)
    first, second = FakeWebSocket(), FakeWebSocket()
    broker.add_client("c1", first)
    broker.add_client("c2", second)

    await broker.subscribe("c1", dead)
    await broker.subscribe("c2", ["PV:LIVE"])
    await broker._drain_nowait()
    assert await wait_for(lambda: _initials_with_meta(second, "PV:LIVE"), timeout=0.2)
    assert not _initial_frames(first)

    await broker.drain_seeds()
    reported = {pv: m["data"][pv]["connected"] for m in _initial_frames(first) for pv in m["data"]}
    assert reported == dict.fromkeys(dead, False)


async def test_channel_with_no_value_within_the_timeout_is_reported_disconnected(fake_connector):
    """A PV whose monitor delivers nothing within ``seed_timeout_s`` is sent as ``connected: false`` without metadata.

    The placeholder is cached, so a later subscriber sees it at once without
    another timer, and nothing is described.
    """
    broker = PVBroker(fake_connector, HareConfig(seed_timeout_s=0.05))
    fake_connector.dead.add("PV:DEAD")
    describes = _record_calls(fake_connector, "describe")
    ws = FakeWebSocket()
    broker.add_client("c1", ws)

    await broker.subscribe("c1", ["PV:DEAD"])
    assert not _initial_frames(ws)
    await settle(broker)

    (initial,) = _initial_frames(ws)
    assert initial["data"]["PV:DEAD"]["connected"] is False
    assert initial["data"]["PV:DEAD"]["error"]
    assert "PV:DEAD" not in initial["meta"]
    assert describes == []
    assert "PV:DEAD" not in broker._seed_retry_after

    late = FakeWebSocket()
    broker.add_client("c2", late)
    await broker.subscribe("c2", ["PV:DEAD"])
    assert "PV:DEAD" not in broker._connect_timers
    (late_initial,) = _initial_frames(late)
    assert late_initial["data"]["PV:DEAD"]["connected"] is False


async def test_subscribers_waiting_on_the_same_channel_share_one_timer(fake_connector):
    """Two clients subscribing a PV with no value yet share one connect timer and both get its report."""
    broker = PVBroker(fake_connector, HareConfig(seed_timeout_s=0.05))
    fake_connector.dead.add("PV:DEAD")
    first, second = FakeWebSocket(), FakeWebSocket()
    broker.add_client("c1", first)
    broker.add_client("c2", second)

    await broker.subscribe("c1", ["PV:DEAD"])
    await broker.subscribe("c2", ["PV:DEAD"])
    assert len(broker._connect_timers) == 1
    await settle(broker)

    for ws in (first, second):
        (initial,) = _initial_frames(ws)
        assert initial["data"]["PV:DEAD"]["connected"] is False


async def test_channel_connecting_after_the_timeout_gets_value_and_metadata(fake_connector):
    """The first connected update of a PV reported disconnected starts its describe; subscribers get value and metadata."""
    broker = PVBroker(fake_connector, HareConfig(seed_timeout_s=0.05))
    fake_connector.dead.add("PV:DEAD")
    describes = _record_calls(fake_connector, "describe")
    ws = FakeWebSocket()
    broker.add_client("c1", ws)
    await broker.subscribe("c1", ["PV:DEAD"])
    await settle(broker)
    assert describes == []

    fake_connector.emit("PV:DEAD", 1.5)
    await settle(broker)

    assert describes == ["PV:DEAD"]
    (with_meta,) = _initials_with_meta(ws, "PV:DEAD")
    assert with_meta["data"]["PV:DEAD"]["value"] == 1.5
    assert with_meta["data"]["PV:DEAD"]["connected"] is True


async def test_first_value_cancels_the_connect_timer(fake_connector):
    """A value arriving before the timeout cancels the timer, so no ``connected: false`` frame follows."""
    broker = PVBroker(fake_connector, HareConfig(seed_timeout_s=0.05))
    fake_connector.dead.add("PV:LATE")
    ws = FakeWebSocket()
    broker.add_client("c1", ws)
    await broker.subscribe("c1", ["PV:LATE"])
    timer = broker._connect_timers["PV:LATE"]

    fake_connector.emit("PV:LATE", 4.0)
    await settle(broker)
    await asyncio.sleep(0.1)

    assert timer.cancelled()
    (initial,) = _initial_frames(ws)
    assert initial["data"]["PV:LATE"]["value"] == 4.0
    assert initial["data"]["PV:LATE"]["connected"] is True


async def test_connect_edge_during_a_pending_initial_send_is_not_lost(fake_connector):
    """A channel that reconnects while its describe task is still sending the previous result is described again.

    The first describe fails, so the task sends a value-only frame; while that
    send is pending the channel drops and reconnects. The running task cannot
    be started again, so it runs once more after the send instead.
    """
    broker = PVBroker(fake_connector, HareConfig(seed_retry_backoff_s=3600))
    describes = _record_calls(fake_connector, "describe", failures=1)
    ws = _GatedWebSocket()
    broker.add_client("c1", ws)

    await broker.subscribe("c1", ["PV:FLAP"])
    await broker._drain_nowait()
    assert await wait_for(lambda: ws.blocked)
    assert describes == ["PV:FLAP"]
    fake_connector.emit("PV:FLAP", 0.0, connected=False)
    fake_connector.emit("PV:FLAP", 2.5)
    await broker._drain_nowait()

    ws.release.set()
    await asyncio.wait_for(broker.drain_seeds(), 1.0)

    assert describes == ["PV:FLAP", "PV:FLAP"]
    (with_meta,) = _initials_with_meta(ws, "PV:FLAP")
    assert with_meta["data"]["PV:FLAP"]["value"] == 2.5
    assert not broker._describe_again


async def test_client_unsubscribing_during_fan_out_receives_no_initial(fake_connector):
    """A client that unsubscribes while an earlier client's send is pending gets nothing from that fan-out."""
    broker = PVBroker(fake_connector, HareConfig())
    first, second = _GatedWebSocket(), _GatedWebSocket()
    broker.add_client("c1", first)
    broker.add_client("c2", second)
    await broker.subscribe("c1", ["PV:SEED"])
    await broker.subscribe("c2", ["PV:SEED"])
    await broker._drain_nowait()

    assert await wait_for(lambda: first.blocked or second.blocked)
    stayer, leaver, leaver_id = (first, second, "c2") if first.blocked else (second, first, "c1")
    await broker.unsubscribe(leaver_id, ["PV:SEED"])
    stayer.release.set()
    # A send to the leaver would block on its unreleased gate and time this out.
    await asyncio.wait_for(broker.drain_seeds(), 1.0)

    assert "PV:SEED" in _initial_frames(stayer)[0]["data"]
    assert not _initial_frames(leaver)
    leaver.release.set()


async def test_connected_updates_do_not_repeat_a_failed_describe(fake_connector):
    """Only the connect edge starts a describe; further connected updates inside the backoff window do not."""
    broker = PVBroker(fake_connector, HareConfig(seed_retry_backoff_s=3600))
    describes = _record_calls(fake_connector, "describe", failures=5)
    broker.add_client("c1", FakeWebSocket())
    await broker.subscribe("c1", ["PV:A"])
    await settle(broker)
    assert describes == ["PV:A"]

    for value in (1.0, 2.0, 3.0):
        fake_connector.emit("PV:A", value)
        await settle(broker)

    assert describes == ["PV:A"]


@pytest.mark.parametrize(
    ("backoff_s", "expected_describes"),
    [pytest.param(3600, 1, id="within-backoff"), pytest.param(0, 2, id="after-backoff")],
)
async def test_failed_describe_is_retried_by_a_later_subscribe_only_after_backoff(
    fake_connector, backoff_s, expected_describes
):
    """A failed describe sets a retry deadline that a later subscribe honours.

    Inside the window the late subscriber gets the cached value without
    metadata; after it, the describe runs again and succeeds.
    """
    broker = PVBroker(fake_connector, HareConfig(seed_retry_backoff_s=backoff_s))
    describes = _record_calls(fake_connector, "describe", failures=1)

    broker.add_client("c1", FakeWebSocket())
    await broker.subscribe("c1", ["PV:META"])
    await settle(broker)
    assert "PV:META" in broker._seed_retry_after

    late = FakeWebSocket()
    broker.add_client("c2", late)
    await broker.subscribe("c2", ["PV:META"])
    await settle(broker)

    assert describes == ["PV:META"] * expected_describes
    assert "PV:META" in _initial_frames(late)[0]["data"]
    assert ("PV:META" in broker._meta) is (expected_describes == 2)
    assert ("PV:META" in broker._seed_retry_after) is (expected_describes == 1)


async def test_stop_monitors_clears_describe_retry_deadline(fake_connector):
    """Releasing a PV drops its retry deadline and seed lock, so re-subscribing describes it at once despite the window."""
    broker = PVBroker(fake_connector, HareConfig(seed_retry_backoff_s=3600))
    describes = _record_calls(fake_connector, "describe", failures=1)

    broker.add_client("c1", FakeWebSocket())
    await broker.subscribe("c1", ["PV:META"])
    await settle(broker)
    assert "PV:META" in broker._seed_retry_after
    assert "PV:META" in broker._seed_locks

    await broker.unsubscribe("c1", ["PV:META"])
    assert "PV:META" not in broker._seed_retry_after
    assert "PV:META" not in broker._seed_locks

    again = FakeWebSocket()
    broker.add_client("c2", again)
    await broker.subscribe("c2", ["PV:META"])
    await settle(broker)

    assert describes == ["PV:META", "PV:META"]
    assert _initials_with_meta(again, "PV:META")


async def test_concurrent_subscribes_share_one_describe(fake_connector):
    """Two clients subscribing a PV while its describe is pending share one call, and both receive the result."""
    broker = PVBroker(fake_connector, HareConfig())
    gate, describes = _gate_describe(fake_connector, {"PV:SEED"})
    first, second = FakeWebSocket(), FakeWebSocket()
    broker.add_client("c1", first)
    broker.add_client("c2", second)

    await broker.subscribe("c1", ["PV:SEED"])
    await broker.subscribe("c2", ["PV:SEED"])
    await broker._drain_nowait()
    assert len(broker._describe_tasks) == 1

    gate.set()
    await broker.drain_seeds()

    assert describes == ["PV:SEED"]
    for ws in (first, second):
        (initial,) = _initial_frames(ws)
        assert "PV:SEED" in initial["data"]
        assert "PV:SEED" in initial["meta"]


async def test_unsubscribe_during_a_pending_describe_leaves_the_leaver_nothing(fake_connector):
    """A client that unsubscribes while the describe is pending gets no frame when it completes; the other does."""
    broker = PVBroker(fake_connector, HareConfig())
    gate, _ = _gate_describe(fake_connector, {"PV:SEED"})
    stayer, leaver = FakeWebSocket(), FakeWebSocket()
    broker.add_client("c1", stayer)
    broker.add_client("c2", leaver)
    await broker.subscribe("c1", ["PV:SEED"])
    await broker.subscribe("c2", ["PV:SEED"])
    await broker._drain_nowait()

    await broker.unsubscribe("c2", ["PV:SEED"])
    gate.set()
    await broker.drain_seeds()

    assert "PV:SEED" in _initial_frames(stayer)[0]["meta"]
    assert not _initial_frames(leaver)
    assert "PV:SEED" not in broker.registry.client_pvs("c2")


async def test_release_cancels_the_pending_timer_and_describe(fake_connector):
    """Releasing PVs cancels their connect timer and pending describe, so nothing is cached and the next subscriber starts afresh."""
    broker = PVBroker(fake_connector, HareConfig(seed_timeout_s=60))
    gate, describes = _gate_describe(fake_connector, {"PV:SLOW"})
    fake_connector.dead.add("PV:DEAD")

    broker.add_client("c1", FakeWebSocket())
    await broker.subscribe("c1", ["PV:DEAD", "PV:SLOW"])
    await broker._drain_nowait()
    await asyncio.sleep(0)
    timer = broker._connect_timers["PV:DEAD"]
    describe = broker._describe_tasks["PV:SLOW"]
    await broker.unsubscribe("c1", ["PV:DEAD", "PV:SLOW"])
    await asyncio.wait([timer, describe], timeout=1.0)

    assert timer.cancelled()
    assert describe.cancelled()
    assert not broker._connect_timers
    assert not broker._describe_tasks
    for pv in ("PV:DEAD", "PV:SLOW"):
        assert pv not in broker._latest
        assert pv not in broker._meta

    gate.set()
    later = FakeWebSocket()
    broker.add_client("c2", later)
    await broker.subscribe("c2", ["PV:SLOW"])
    await settle(broker)
    assert describes == ["PV:SLOW", "PV:SLOW"]
    assert _initials_with_meta(later, "PV:SLOW")


async def test_describe_finishing_after_release_leaves_no_cached_state(fake_connector):
    """A describe that outlives its PV's release caches nothing once it finishes.

    The describe ignores the cancellation the release sends, standing in for a
    connector call that cannot be aborted; the stale seed lock keeps its late
    result out of the cache, and the next subscriber describes the PV afresh.
    """
    broker = PVBroker(fake_connector, HareConfig(seed_retry_backoff_s=3600))
    original = fake_connector.describe
    release = asyncio.Event()
    describes: list[str] = []

    async def stubborn_describe(pv):
        describes.append(pv)
        await _uncancellable(release)()
        return await original(pv)

    fake_connector.describe = stubborn_describe

    broker.add_client("c1", FakeWebSocket())
    await broker.subscribe("c1", ["PV:SEED"])
    await broker._drain_nowait()
    await asyncio.sleep(0)
    orphaned = broker._describe_tasks["PV:SEED"]
    await broker.unsubscribe("c1", ["PV:SEED"])
    release.set()
    await asyncio.wait_for(orphaned, 1.0)

    assert "PV:SEED" not in broker._latest
    assert "PV:SEED" not in broker._meta
    assert "PV:SEED" not in broker._seed_retry_after

    later = FakeWebSocket()
    broker.add_client("c2", later)
    await broker.subscribe("c2", ["PV:SEED"])
    await settle(broker)
    assert describes == ["PV:SEED", "PV:SEED"]
    assert _initials_with_meta(later, "PV:SEED")


async def test_orphaned_describe_finishing_last_keeps_the_newer_metadata(fake_connector):
    """A released PV's describe that finishes after a re-subscribe's describe does not overwrite it.

    The first describe ignores its cancellation and is released only after the
    second has completed, so the orphaned attempt finishes last; the cache
    keeps the second result.
    """
    broker = PVBroker(fake_connector, HareConfig(seed_retry_backoff_s=3600))
    gates = [asyncio.Event(), asyncio.Event()]
    calls = 0

    async def gated_describe(pv):
        nonlocal calls
        index = calls
        calls += 1
        await _uncancellable(gates[index])()
        return PVMeta(pv=pv, units=f"u{index + 1}")

    fake_connector.describe = gated_describe

    broker.add_client("c1", FakeWebSocket())
    await broker.subscribe("c1", ["PV:SEED"])
    await broker._drain_nowait()
    await asyncio.sleep(0)
    orphaned = broker._describe_tasks["PV:SEED"]
    await broker.unsubscribe("c1", ["PV:SEED"])
    broker.add_client("c2", FakeWebSocket())
    await broker.subscribe("c2", ["PV:SEED"])
    await broker._drain_nowait()
    await asyncio.sleep(0)
    assert calls == 2

    gates[1].set()
    await broker.drain_seeds()
    assert broker._meta["PV:SEED"].units == "u2"

    gates[0].set()
    await asyncio.wait_for(orphaned, 1.0)
    assert broker._meta["PV:SEED"].units == "u2"


async def test_describe_queued_for_a_slot_skips_a_pv_released_meanwhile(fake_connector):
    """A describe still waiting for a ``seed_concurrency`` slot when its PV is released is cancelled and caches nothing.

    With one slot, PV:B's describe queues behind PV:A's pending one and PV:B is
    released before the slot frees, so the next subscriber of PV:B describes it afresh.
    """
    broker = PVBroker(fake_connector, HareConfig(seed_concurrency=1))
    gate, describes = _gate_describe(fake_connector, {"PV:A"})

    broker.add_client("c1", FakeWebSocket())
    await broker.subscribe("c1", ["PV:A", "PV:B"])
    await broker._drain_nowait()
    await asyncio.sleep(0)
    assert describes == ["PV:A"]
    await broker.unsubscribe("c1", ["PV:B"])
    gate.set()
    await broker.drain_seeds()

    assert describes == ["PV:A"]
    assert "PV:B" not in broker._latest
    assert "PV:B" not in broker._meta
    assert "PV:B" not in broker._seed_locks
    assert "PV:B" not in broker._describe_tasks

    later = FakeWebSocket()
    broker.add_client("c2", later)
    await broker.subscribe("c2", ["PV:B"])
    await settle(broker)
    assert describes == ["PV:A", "PV:B"]
    assert _initials_with_meta(later, "PV:B")


async def test_stop_cancels_pending_timers_and_describes(fake_connector):
    """``stop`` cancels connect timers and describes still in flight rather than waiting for them."""
    broker = PVBroker(fake_connector, HareConfig(seed_timeout_s=60))
    _gate_describe(fake_connector, {"PV:SLOW"})
    fake_connector.dead.add("PV:DEAD")
    broker.add_client("c1", FakeWebSocket())
    await broker.subscribe("c1", ["PV:DEAD", "PV:SLOW"])
    await broker._drain_nowait()
    await asyncio.sleep(0)
    timer = broker._connect_timers["PV:DEAD"]
    describe = broker._describe_tasks["PV:SLOW"]

    await asyncio.wait_for(broker.stop(), 1.0)

    assert timer.cancelled()
    assert describe.cancelled()
    assert not broker._connect_timers
    assert not broker._describe_tasks


async def test_monitor_update_queued_before_release_is_not_cached(broker, fake_connector):
    """A monitor update still queued when its PV is released is discarded when drained.

    Nothing is cached or buffered for the released PV, so a later read goes to
    the connector instead of serving the update from before the release.
    """
    broker.add_client("c1", FakeWebSocket())
    await broker.subscribe("c1", ["PV:A"])
    fake_connector.emit("PV:A", 7.0)
    await broker.unsubscribe("c1", ["PV:A"])
    await broker._drain_nowait()

    assert "PV:A" not in broker._latest
    assert "PV:A" not in broker._buffer
    assert (await broker.get_value("PV:A")).value == 0.0


async def test_released_monitor_update_delivered_after_resubscribe_is_not_cached(broker, fake_connector):
    """An update from a released monitor that arrives after a re-subscribe does not replace the fresh seed.

    The EPICS backend hands updates to the loop with ``call_soon_threadsafe``, so
    the old monitor's callback can still run once a new subscribe has seeded the
    PV and started a new monitor. Only the new monitor's updates are cached.
    """
    broker.add_client("c1", FakeWebSocket())
    await broker.subscribe("c1", ["PV:A"])
    await settle(broker)
    released_callback = fake_connector.callbacks["PV:A"]
    await broker.unsubscribe("c1", ["PV:A"])
    broker.add_client("c2", FakeWebSocket())
    await broker.subscribe("c2", ["PV:A"])
    await settle(broker)

    released_callback(PVValue(pv="PV:A", value=7.0, severity=AlarmSeverity.NO_ALARM, ts_ns=2, connected=True))
    await broker._drain_nowait()
    assert broker._latest["PV:A"].value == 0.0
    assert "PV:A" not in broker._buffer

    fake_connector.emit("PV:A", 3.0)
    await broker._drain_nowait()
    assert broker._latest["PV:A"].value == 3.0
    assert broker._buffer["PV:A"].value == 3.0
