"""Tests for Fox's SubscriptionManager live subscription orchestrator."""

import asyncio

import pytest

from canopy.fox.config import FoxConfig
from canopy.fox.errors import (
    FormulaTypeError,
    FormulaInputMissing,
    SubscriptionLimitExceeded,
)
from canopy.core.errors import ErrorCode
from canopy.contracts.formula import FormulaSpec
from tests.canopy.fox.conftest import FakeDataSource
from canopy.fox.services.parser import Parser
from canopy.vole.services.notifier import ChangeNotifier
from canopy.fox.services.formula_service import FormulaService
from canopy.fox.services.subscription_manager import SubscriptionManager


@pytest.fixture
def notifier():
    return ChangeNotifier()


@pytest.fixture
def fake_data_source():
    from canopy.contracts.sample import Sample, AlarmSeverity

    return FakeDataSource(
        latest={
            "PV:A": Sample(ts_ns=1000, value=5.0, severity=AlarmSeverity.NO_ALARM, source="archive"),
            "PV:B": Sample(ts_ns=2000, value=3.0, severity=AlarmSeverity.NO_ALARM, source="archive"),
        }
    )


@pytest.fixture
def formula_service(fake_data_source):
    return FormulaService(fake_data_source, Parser(), FoxConfig())


@pytest.fixture
def manager(formula_service, notifier):
    subscribed_pvs = []

    async def subscribe_live(pvs: list[str]) -> None:
        subscribed_pvs.extend(pvs)

    mgr = SubscriptionManager(
        formula_service=formula_service,
        on_change_fn=notifier.subscribe,
        subscribe_live_fn=subscribe_live,
    )
    mgr._subscribed_pvs = subscribed_pvs
    return mgr


def _make_collector() -> tuple[list[dict], "asyncio.Callable"]:
    messages: list[dict] = []

    async def send_fn(msg: dict) -> None:
        messages.append(msg)

    return messages, send_fn


def _scalar_spec() -> FormulaSpec:
    return FormulaSpec(
        kind="scalar",
        expression="{a} + {b}",
        bindings={"a": "PV:A", "b": "PV:B"},
    )


async def test_add_subscription_leaves_no_orphan_on_start_failure(formula_service, notifier):
    # If _start_shared_eval raises, no task-less SharedEval may linger -- a later
    # subscriber with the same key would silently attach to a dead eval.
    async def boom(pvs: list[str]) -> None:
        raise RuntimeError("subscribe failed")

    mgr = SubscriptionManager(
        formula_service=formula_service,
        on_change_fn=notifier.subscribe,
        subscribe_live_fn=boom,
        subscribe_latest_fn=boom,
    )
    _messages, send_fn = _make_collector()

    with pytest.raises(RuntimeError):
        await mgr.add_subscription("c1", "s1", _scalar_spec(), t0_ns=0, t1_ns=0, send_fn=send_fn)

    assert mgr._shared_evals == {}  # no orphan
    assert mgr._client_subs.get("c1", {}) == {}  # sub not registered


async def test_update_subscription_preserves_old_eval_on_start_failure(formula_service, notifier):
    # If starting the new eval fails, update_subscription must leave the old eval
    # and the sub->key mapping intact rather than orphaning the subscription on a
    # torn-down key. subscribe_live only fails on the *second* call (the update).
    calls = {"n": 0}

    async def subscribe_live(pvs: list[str]) -> None:
        calls["n"] += 1
        if calls["n"] > 1:
            raise RuntimeError("subscribe failed")

    mgr = SubscriptionManager(
        formula_service=formula_service,
        on_change_fn=notifier.subscribe,
        subscribe_live_fn=subscribe_live,
        subscribe_latest_fn=subscribe_live,
    )
    _messages, send_fn = _make_collector()

    await mgr.add_subscription("c1", "s1", _scalar_spec(), t0_ns=1000, t1_ns=0, send_fn=send_fn)
    old_key = mgr._client_subs["c1"]["s1"]

    with pytest.raises(RuntimeError):
        await mgr.update_subscription("c1", "s1", send_fn, t0_ns=-5000)

    # Old eval survives; the mapping still points at it (not orphaned).
    assert mgr._client_subs["c1"]["s1"] == old_key
    assert old_key in mgr._shared_evals
    await mgr.shutdown()


def _timeseries_spec() -> FormulaSpec:
    return FormulaSpec(
        kind="timeseries",
        expression="{a} + {b}",
        bindings={"a": "PV:A", "b": "PV:B"},
    )


class TestResourceCaps:
    async def test_interval_is_floored_to_min(self, formula_service, notifier):
        async def sub_live(pvs: list[str]) -> None:
            pass

        mgr = SubscriptionManager(
            formula_service=formula_service,
            on_change_fn=notifier.subscribe,
            subscribe_live_fn=sub_live,
            min_interval_ms=50,
        )
        _messages, send_fn = _make_collector()
        await mgr.add_subscription("c1", "s1", _scalar_spec(), 0, 0, send_fn, interval_ms=1)
        key = mgr._client_subs["c1"]["s1"]
        assert mgr._shared_evals[key].interval_ms == 50
        await mgr.shutdown()

    async def test_update_interval_is_floored_to_min(self, formula_service, notifier):
        # A client can't dodge the floor by subscribing legitimately and then
        # updating to a CPU-pinning interval.
        async def sub_live(pvs: list[str]) -> None:
            pass

        mgr = SubscriptionManager(
            formula_service=formula_service,
            on_change_fn=notifier.subscribe,
            subscribe_live_fn=sub_live,
            min_interval_ms=50,
        )
        _messages, send_fn = _make_collector()
        await mgr.add_subscription("c1", "s1", _scalar_spec(), 0, 0, send_fn, interval_ms=100)
        ok = await mgr.update_subscription("c1", "s1", send_fn, interval_ms=1)
        assert ok is True
        key = mgr._client_subs["c1"]["s1"]
        assert mgr._shared_evals[key].interval_ms == 50
        await mgr.shutdown()

    async def test_per_client_subscription_cap(self, formula_service, notifier):
        async def sub_live(pvs: list[str]) -> None:
            pass

        mgr = SubscriptionManager(
            formula_service=formula_service,
            on_change_fn=notifier.subscribe,
            subscribe_live_fn=sub_live,
            max_subscriptions_per_client=2,
        )
        _messages, send_fn = _make_collector()
        await mgr.add_subscription("c1", "s1", _scalar_spec(), 0, 0, send_fn)
        await mgr.add_subscription("c1", "s2", _timeseries_spec(), 1000, 0, send_fn)
        with pytest.raises(SubscriptionLimitExceeded):
            await mgr.add_subscription("c1", "s3", _scalar_spec(), 0, 0, send_fn, interval_ms=100)
        await mgr.shutdown()

    async def test_resubscribe_same_id_not_blocked_by_cap(self, formula_service, notifier):
        async def sub_live(pvs: list[str]) -> None:
            pass

        mgr = SubscriptionManager(
            formula_service=formula_service,
            on_change_fn=notifier.subscribe,
            subscribe_live_fn=sub_live,
            max_subscriptions_per_client=1,
        )
        _messages, send_fn = _make_collector()
        await mgr.add_subscription("c1", "s1", _scalar_spec(), 0, 0, send_fn)
        # Replacing the same sub_id must not trip the cap.
        await mgr.add_subscription("c1", "s1", _timeseries_spec(), 1000, 0, send_fn)
        assert list(mgr._client_subs["c1"].keys()) == ["s1"]
        await mgr.shutdown()


class TestSubscriptionManager:
    async def test_add_subscription_triggers_initial_eval(self, manager):
        messages, send_fn = _make_collector()

        await manager.add_subscription("client-1", "sub-1", _scalar_spec(), 0, 0, send_fn)
        await asyncio.sleep(0.05)

        assert len(messages) >= 1
        assert messages[0]["type"] == "result"
        assert messages[0]["id"] == "sub-1"
        assert "data" in messages[0]

        await manager.shutdown()

    async def test_notification_triggers_reevaluation(self, manager, notifier):
        messages, send_fn = _make_collector()

        await manager.add_subscription("client-1", "sub-1", _scalar_spec(), 0, 0, send_fn)
        await asyncio.sleep(0.05)

        initial_count = len(messages)
        notifier.notify("PV:A")
        await asyncio.sleep(0.05)

        assert len(messages) > initial_count
        assert messages[-1]["type"] == "result"

        await manager.shutdown()

    async def test_coalescing_rapid_notifications(self, manager, notifier):
        messages, send_fn = _make_collector()

        await manager.add_subscription("client-1", "sub-1", _timeseries_spec(), 1000, 0, send_fn)
        await asyncio.sleep(0.05)
        initial_count = len(messages)

        for _ in range(10):
            notifier.notify("PV:A")
        await asyncio.sleep(0.1)

        new_results = [m for m in messages[initial_count:] if m["type"] == "result"]
        assert len(new_results) >= 1
        assert len(new_results) <= 3

        await manager.shutdown()

    async def test_remove_subscription_cancels_task(self, manager, notifier):
        messages, send_fn = _make_collector()

        await manager.add_subscription("client-1", "sub-1", _scalar_spec(), 0, 0, send_fn)
        await asyncio.sleep(0.05)

        await manager.remove_subscription("client-1", "sub-1")
        count_after_remove = len(messages)

        notifier.notify("PV:A")
        await asyncio.sleep(0.05)

        assert len(messages) == count_after_remove

    async def test_remove_client_cleans_all_subs(self, manager, notifier):
        messages_1, send_fn_1 = _make_collector()
        messages_2, send_fn_2 = _make_collector()

        await manager.add_subscription("client-1", "sub-1", _scalar_spec(), 0, 0, send_fn_1)
        await manager.add_subscription("client-1", "sub-2", _timeseries_spec(), 1000, 5000, send_fn_2)
        await asyncio.sleep(0.05)

        await manager.remove_client("client-1")
        assert "client-1" not in manager._client_subs

        notifier.notify("PV:A")
        await asyncio.sleep(0.05)

        count_1 = len(messages_1)
        count_2 = len(messages_2)
        notifier.notify("PV:B")
        await asyncio.sleep(0.05)

        assert len(messages_1) == count_1
        assert len(messages_2) == count_2

    async def test_eval_error_sends_error_frame_loop_continues(self, manager, notifier):
        messages, send_fn = _make_collector()

        spec = FormulaSpec(
            kind="scalar",
            expression="{a} +",
            bindings={"a": "PV:A"},
        )

        await manager.add_subscription("client-1", "sub-1", spec, 0, 0, send_fn)
        await asyncio.sleep(0.05)

        assert len(messages) >= 1
        assert messages[0]["type"] == "error"
        assert "code" in messages[0]

        await manager.shutdown()

    async def test_shutdown_cancels_everything(self, manager, notifier):
        messages, send_fn = _make_collector()

        await manager.add_subscription("client-1", "sub-1", _scalar_spec(), 0, 0, send_fn)
        await manager.add_subscription("client-2", "sub-2", _timeseries_spec(), 1000, 5000, send_fn)
        await asyncio.sleep(0.05)

        await manager.shutdown()
        assert len(manager._client_subs) == 0

    async def test_resubscribe_same_id_replaces(self, manager, notifier):
        messages, send_fn = _make_collector()

        await manager.add_subscription("client-1", "sub-1", _scalar_spec(), 0, 0, send_fn)
        await asyncio.sleep(0.05)

        await manager.add_subscription("client-1", "sub-1", _timeseries_spec(), 1000, 0, send_fn)
        await asyncio.sleep(0.05)

        subs = manager._client_subs.get("client-1", {})
        assert "sub-1" in subs
        key = subs["sub-1"]
        shared = manager._shared_evals[key]
        assert shared.spec.kind == "timeseries"

        await manager.shutdown()

    async def test_poll_mode_fires_on_interval(self, manager, notifier):
        messages, send_fn = _make_collector()

        await manager.add_subscription("client-1", "sub-1", _scalar_spec(), 0, 0, send_fn, interval_ms=50)
        await asyncio.sleep(0.18)

        results = [m for m in messages if m["type"] == "result"]
        assert len(results) >= 3

        await manager.shutdown()

    async def test_poll_mode_no_change_notifications(self, manager, notifier):
        messages, send_fn = _make_collector()

        await manager.add_subscription("client-1", "sub-1", _scalar_spec(), 0, 0, send_fn, interval_ms=500)
        await asyncio.sleep(0.02)

        key = manager._client_subs["client-1"]["sub-1"]
        shared = manager._shared_evals[key]
        assert shared.unsub_fns == []

        await manager.shutdown()

    async def test_deduplication_shares_eval(self, manager, notifier):
        messages_1, send_fn_1 = _make_collector()
        messages_2, send_fn_2 = _make_collector()

        await manager.add_subscription("client-1", "sub-1", _scalar_spec(), 0, 0, send_fn_1)
        await manager.add_subscription("client-2", "sub-2", _scalar_spec(), 0, 0, send_fn_2)

        assert len(manager._shared_evals) == 1

        await manager.shutdown()

    async def test_deduplication_fan_out(self, manager, notifier):
        messages_1, send_fn_1 = _make_collector()
        messages_2, send_fn_2 = _make_collector()

        await manager.add_subscription("client-1", "sub-1", _scalar_spec(), 0, 0, send_fn_1)
        await manager.add_subscription("client-2", "sub-2", _scalar_spec(), 0, 0, send_fn_2)
        await asyncio.sleep(0.05)

        results_1 = [m for m in messages_1 if m["type"] == "result"]
        results_2 = [m for m in messages_2 if m["type"] == "result"]
        assert len(results_1) >= 1
        assert len(results_2) >= 1
        assert results_1[0]["id"] == "sub-1"
        assert results_2[0]["id"] == "sub-2"

        await manager.shutdown()

    async def test_last_listener_removed_tears_down_shared(self, manager, notifier):
        messages, send_fn = _make_collector()

        await manager.add_subscription("client-1", "sub-1", _scalar_spec(), 0, 0, send_fn)
        await asyncio.sleep(0.05)
        assert len(manager._shared_evals) == 1

        await manager.remove_subscription("client-1", "sub-1")
        assert len(manager._shared_evals) == 0

    async def test_different_params_not_deduplicated(self, manager, notifier):
        messages_1, send_fn_1 = _make_collector()
        messages_2, send_fn_2 = _make_collector()

        await manager.add_subscription("client-1", "sub-1", _scalar_spec(), 0, 0, send_fn_1)
        await manager.add_subscription("client-2", "sub-2", _scalar_spec(), 0, 0, send_fn_2, interval_ms=100)

        assert len(manager._shared_evals) == 2

        await manager.shutdown()

    async def test_update_subscription_changes_params(self, manager, notifier):
        messages, send_fn = _make_collector()

        await manager.add_subscription("client-1", "sub-1", _scalar_spec(), 0, 0, send_fn)
        await asyncio.sleep(0.05)

        old_key = manager._client_subs["client-1"]["sub-1"]
        ok = await manager.update_subscription("client-1", "sub-1", send_fn, t0_ns=-5000)
        assert ok is True

        new_key = manager._client_subs["client-1"]["sub-1"]
        assert new_key != old_key

        shared = manager._shared_evals[new_key]
        assert shared.t0_ns == -5000

        await manager.shutdown()

    async def test_update_shared_subscription_detaches(self, manager, notifier):
        messages_1, send_fn_1 = _make_collector()
        messages_2, send_fn_2 = _make_collector()

        await manager.add_subscription("client-1", "sub-1", _scalar_spec(), 0, 0, send_fn_1)
        await manager.add_subscription("client-2", "sub-2", _scalar_spec(), 0, 0, send_fn_2)
        await asyncio.sleep(0.05)

        assert len(manager._shared_evals) == 1

        ok = await manager.update_subscription("client-1", "sub-1", send_fn_1, interval_ms=100)
        assert ok is True

        # Now there are two shared evals: original (client-2) and new (client-1)
        assert len(manager._shared_evals) == 2

        await manager.shutdown()

    async def test_update_nonexistent_subscription(self, manager, notifier):
        messages, send_fn = _make_collector()

        ok = await manager.update_subscription("client-1", "no-such-sub", send_fn, t0_ns=-1000)
        assert ok is False


async def test_late_joiner_of_existing_eval_receives_a_result(manager):
    """A subscription joining a running push-mode eval gets a result without a PV change.

    The shared eval's initial evaluation goes to the first listener and static
    inputs may never fire another change, so the joiner's first result comes
    from its own catch-up evaluation.
    """
    first, first_send = _make_collector()
    await manager.add_subscription("c1", "s1", _scalar_spec(), t0_ns=0, t1_ns=0, send_fn=first_send)
    await asyncio.sleep(0.05)
    assert any(m["type"] == "result" for m in first)

    late, late_send = _make_collector()
    await manager.add_subscription("c2", "s2", _scalar_spec(), t0_ns=0, t1_ns=0, send_fn=late_send)
    await asyncio.sleep(0.1)
    assert any(m["type"] == "result" for m in late), "late joiner never got a result"

    await manager.shutdown()


async def test_late_join_does_not_duplicate_result_for_existing_listener(manager):
    """A late joiner's catch-up sends nothing to listeners that already hold a result.

    The catch-up evaluation targets only the joining listener, so the first
    listener keeps exactly one result.
    """
    first, first_send = _make_collector()
    await manager.add_subscription("c1", "s1", _scalar_spec(), t0_ns=0, t1_ns=0, send_fn=first_send)
    await asyncio.sleep(0.05)
    assert sum(1 for m in first if m["type"] == "result") == 1

    late, late_send = _make_collector()
    await manager.add_subscription("c2", "s2", _scalar_spec(), t0_ns=0, t1_ns=0, send_fn=late_send)
    await asyncio.sleep(0.1)

    assert sum(1 for m in first if m["type"] == "result") == 1, (
        "c1 received a duplicate result triggered by c2's late join"
    )
    await manager.shutdown()


async def test_all_dead_listeners_retire_the_shared_eval(manager, notifier):
    """A shared eval whose every result send fails is torn down and unregistered.

    The retired eval is absent from the deduplication map, so a fresh
    subscriber with the same parameters starts a working eval instead of
    joining a loop whose clients are all gone.
    """

    async def dead_send(_msg: dict) -> None:
        raise RuntimeError("socket closed")

    await manager.add_subscription("c1", "s1", _scalar_spec(), t0_ns=0, t1_ns=0, send_fn=dead_send)
    await asyncio.sleep(0.1)
    assert manager._shared_evals == {}
    assert manager._client_subs.get("c1", {}) == {}

    fresh, fresh_send = _make_collector()
    await manager.add_subscription("c2", "s2", _scalar_spec(), t0_ns=0, t1_ns=0, send_fn=fresh_send)
    await asyncio.sleep(0.1)
    assert any(m["type"] == "result" for m in fresh)

    await manager.shutdown()


async def test_all_dead_listeners_retire_on_the_error_path_too(manager):
    """A shared eval that only ever errors is retired once every error send fails.

    Such an eval never reaches the result fan-out, so the error fan-out drops
    failed listeners and retires the eval the same way.
    """
    bad_spec = FormulaSpec(kind="scalar", expression="{a} +", bindings={"a": "PV:A"})

    async def dead_send(_msg: dict) -> None:
        raise RuntimeError("socket closed")

    await manager.add_subscription("c1", "s1", bad_spec, t0_ns=0, t1_ns=0, send_fn=dead_send)
    await asyncio.sleep(0.1)

    assert manager._shared_evals == {}
    assert manager._client_subs.get("c1", {}) == {}

    await manager.shutdown()


async def test_shutdown_cancels_an_in_flight_catch_up(manager):
    """``shutdown`` cancels a catch-up evaluation that is still in flight.

    The catch-up task is tracked on its SharedEval, so it never sends to a
    client after shutdown.
    """
    _, first_send = _make_collector()
    await manager.add_subscription("c1", "s1", _scalar_spec(), t0_ns=0, t1_ns=0, send_fn=first_send)
    await asyncio.sleep(0.05)

    gate = asyncio.Event()
    real_evaluate = manager._formula_service.evaluate

    async def slow_evaluate(*args, **kwargs):
        await gate.wait()
        return await real_evaluate(*args, **kwargs)

    manager._formula_service.evaluate = slow_evaluate

    late, late_send = _make_collector()
    await manager.add_subscription("c2", "s2", _scalar_spec(), t0_ns=0, t1_ns=0, send_fn=late_send)
    await asyncio.sleep(0.02)

    await manager.shutdown()
    gate.set()
    await asyncio.sleep(0.1)

    assert not late, "catch-up delivered a message after shutdown()"


async def test_shutdown_stops_retries_handed_over_by_a_catch_up(manager):
    """``shutdown`` stops the retrying that a catch-up hands to the shared eval on missing inputs.

    Shutdown leaves the listener list intact, so nothing but cancelling the
    shared eval's loop ends the retries.
    """
    _, first_send = _make_collector()
    await manager.add_subscription("c1", "s1", _scalar_spec(), t0_ns=0, t1_ns=0, send_fn=first_send)
    await asyncio.sleep(0.05)

    calls = 0

    async def always_retriable(*args, **kwargs):
        nonlocal calls
        calls += 1
        raise FormulaInputMissing("input not ready")

    manager._formula_service.evaluate = always_retriable

    _, late_send = _make_collector()
    await manager.add_subscription("c2", "s2", _scalar_spec(), t0_ns=0, t1_ns=0, send_fn=late_send)
    await asyncio.sleep(0.6)

    await manager.shutdown()
    settled = calls
    await asyncio.sleep(0.6)

    assert calls == settled, "evaluations continued after shutdown()"


def _scalar_values(messages: list[dict]) -> list:
    """Extract the scalar value of every result frame, in arrival order.

    Parameters
    ----------
    messages : list of dict
        Frames collected by a `_make_collector` send function.

    Returns
    -------
    list
        ``data.scalar.value`` of each ``result`` frame.
    """
    return [m["data"]["scalar"]["value"] for m in messages if m["type"] == "result"]


async def test_catch_up_result_does_not_overwrite_a_newer_fan_out(manager, notifier, fake_data_source):
    """A catch-up result that finishes after a fresher fan-out is discarded.

    The joiner's catch-up evaluation reads the old inputs and stalls; a PV
    change then fans the fresh value out to every listener. When the stalled
    catch-up completes, the joiner has already been served by the shared eval,
    so it keeps exactly one result carrying the fresh value.
    """
    _, first_send = _make_collector()
    await manager.add_subscription("c1", "s1", _scalar_spec(), t0_ns=0, t1_ns=0, send_fn=first_send)
    await asyncio.sleep(0.05)

    real_evaluate = manager._formula_service.evaluate
    stale_read_done = asyncio.Event()
    release = asyncio.Event()
    gate_armed = True

    async def first_call_stalls_after_reading(*args, **kwargs):
        nonlocal gate_armed
        result = await real_evaluate(*args, **kwargs)
        if gate_armed:
            gate_armed = False
            stale_read_done.set()
            await release.wait()
        return result

    manager._formula_service.evaluate = first_call_stalls_after_reading

    late, late_send = _make_collector()
    await manager.add_subscription("c2", "s2", _scalar_spec(), t0_ns=0, t1_ns=0, send_fn=late_send)
    await asyncio.wait_for(stale_read_done.wait(), 1.0)

    fake_data_source.latest["PV:A"] = fake_data_source.latest["PV:A"].model_copy(update={"ts_ns": 5000, "value": 10.0})
    notifier.notify("PV:A")
    await asyncio.sleep(0.05)
    assert _scalar_values(late) == [13.0]

    release.set()
    await asyncio.sleep(0.05)

    assert _scalar_values(late) == [13.0], "stale catch-up result overwrote the fresher fan-out"
    await manager.shutdown()


async def test_catch_up_does_not_send_behind_an_in_flight_fan_out(manager, notifier, fake_data_source):
    """A catch-up that finishes while a fresher fan-out is mid-send to the joiner is discarded.

    The joiner's send function serialises frames through its own lock, as the
    WebSocket endpoint does, and stalls while sending the fanned-out fresh
    value. The stale catch-up completes during that stall, before the joiner
    is marked delivered, and must not queue its older value behind the fresh
    one.
    """
    _, first_send = _make_collector()
    await manager.add_subscription("c1", "s1", _scalar_spec(), t0_ns=0, t1_ns=0, send_fn=first_send)
    await asyncio.sleep(0.05)

    real_evaluate = manager._formula_service.evaluate
    stale_read_done = asyncio.Event()
    release_catch_up = asyncio.Event()
    gate_armed = True

    async def first_call_stalls_after_reading(*args, **kwargs):
        nonlocal gate_armed
        result = await real_evaluate(*args, **kwargs)
        if gate_armed:
            gate_armed = False
            stale_read_done.set()
            await release_catch_up.wait()
        return result

    manager._formula_service.evaluate = first_call_stalls_after_reading

    late: list[dict] = []
    send_lock = asyncio.Lock()
    fan_out_sending = asyncio.Event()
    release_fan_out = asyncio.Event()

    async def late_send(msg: dict) -> None:
        async with send_lock:
            late.append(msg)
            if msg["type"] == "result" and msg["data"]["scalar"]["value"] == 13.0:
                fan_out_sending.set()
                await release_fan_out.wait()

    await manager.add_subscription("c2", "s2", _scalar_spec(), t0_ns=0, t1_ns=0, send_fn=late_send)
    await asyncio.wait_for(stale_read_done.wait(), 1.0)

    fake_data_source.latest["PV:A"] = fake_data_source.latest["PV:A"].model_copy(update={"ts_ns": 5000, "value": 10.0})
    notifier.notify("PV:A")
    await asyncio.wait_for(fan_out_sending.wait(), 1.0)

    release_catch_up.set()
    await asyncio.sleep(0.05)
    release_fan_out.set()
    await asyncio.sleep(0.05)

    assert _scalar_values(late) == [13.0], "stale catch-up result was sent after the in-flight fan-out"
    await manager.shutdown()


async def test_catch_up_hands_a_retriable_error_to_the_shared_eval(manager, monkeypatch):
    """A catch-up that hits a retriable input error evaluates once, re-arms the shared eval and ends.

    The evaluation stub records which task called it; the catch-up task calls
    it exactly once, and the shared eval's own loop does the retrying.
    """
    monkeypatch.setattr("canopy.fox.services.subscription_manager._INPUT_RETRY_DELAY_S", 0.01)
    _, first_send = _make_collector()
    await manager.add_subscription("c1", "s1", _scalar_spec(), t0_ns=0, t1_ns=0, send_fn=first_send)
    await asyncio.sleep(0.05)
    shared = manager._shared_evals[manager._client_subs["c1"]["s1"]]

    callers: list[asyncio.Task] = []

    async def missing(*args, **kwargs):
        callers.append(asyncio.current_task())
        raise FormulaInputMissing("input not ready")

    manager._formula_service.evaluate = missing

    _, late_send = _make_collector()
    await manager.add_subscription("c2", "s2", _scalar_spec(), t0_ns=0, t1_ns=0, send_fn=late_send)
    (catch_up,) = shared.catch_up_tasks
    await asyncio.wait_for(catch_up, 1.0)
    await asyncio.sleep(0.05)

    assert callers.count(catch_up) == 1
    assert callers.count(shared.task) > 1, "the shared eval did not take over the retries"
    await manager.shutdown()


async def test_joiners_of_an_eval_without_output_share_its_first_result(manager, fake_data_source, monkeypatch):
    """Joiners of an eval that has fanned out nothing get no catch-up and receive its first result.

    While an input is missing the shared eval retries alone, so the evaluation
    rate stays at that of a single loop however many listeners join. Once the
    input arrives, the retrying loop picks it up and every listener receives
    the same first result exactly once.
    """
    from canopy.contracts.sample import Sample, AlarmSeverity

    monkeypatch.setattr("canopy.fox.services.subscription_manager._INPUT_RETRY_DELAY_S", 0.01)
    spec = FormulaSpec(kind="scalar", expression="{a} + {c}", bindings={"a": "PV:A", "c": "PV:C"})
    real_evaluate = manager._formula_service.evaluate
    calls = 0

    async def counting_evaluate(*args, **kwargs):
        nonlocal calls
        calls += 1
        return await real_evaluate(*args, **kwargs)

    manager._formula_service.evaluate = counting_evaluate
    collected = []
    for i in range(10):
        messages, send_fn = _make_collector()
        collected.append(messages)
        await manager.add_subscription(f"c{i}", "s", spec, t0_ns=0, t1_ns=0, send_fn=send_fn)
    shared = manager._shared_evals[manager._client_subs["c0"]["s"]]
    assert shared.catch_up_tasks == set()

    calls = 0
    await asyncio.sleep(0.1)
    assert calls <= 20, f"{calls} evaluations in 0.1 s; expected one retry loop of about 10"

    fake_data_source.latest["PV:C"] = Sample(ts_ns=3000, value=1.0, severity=AlarmSeverity.NO_ALARM, source="archive")
    await asyncio.sleep(0.05)

    assert [_scalar_values(messages) for messages in collected] == [[6.0]] * 10
    await manager.shutdown()


async def test_resubscribe_during_a_catch_up_delivers_one_result(manager):
    """Re-subscribing an id while its catch-up is in flight leaves only the new listener's catch-up to send.

    The replaced listener's catch-up finds its own listener detached, even
    though the new listener carries the same client, id and send function, so
    the client receives a single result.
    """
    _, first_send = _make_collector()
    await manager.add_subscription("c0", "s0", _scalar_spec(), t0_ns=0, t1_ns=0, send_fn=first_send)
    await asyncio.sleep(0.05)

    real_evaluate = manager._formula_service.evaluate
    release = asyncio.Event()
    gate_armed = True

    async def first_call_stalls(*args, **kwargs):
        nonlocal gate_armed
        result = await real_evaluate(*args, **kwargs)
        if gate_armed:
            gate_armed = False
            await release.wait()
        return result

    manager._formula_service.evaluate = first_call_stalls

    late, late_send = _make_collector()
    await manager.add_subscription("c1", "s1", _scalar_spec(), t0_ns=0, t1_ns=0, send_fn=late_send)
    await asyncio.sleep(0.02)
    await manager.add_subscription("c1", "s1", _scalar_spec(), t0_ns=0, t1_ns=0, send_fn=late_send)
    await asyncio.sleep(0.05)
    release.set()
    await asyncio.sleep(0.05)

    assert _scalar_values(late) == [8.0]
    await manager.shutdown()


async def test_fan_out_skips_a_listener_updated_away_mid_send(manager, notifier, fake_data_source):
    """A fan-out already under way sends nothing to a listener updated to another formula meanwhile.

    The first listener's send stalls mid fan-out while the second listener's
    subscription is updated from the sum to the product, so its last frame
    stays the product's value rather than the sum's.
    """
    product = FormulaSpec(kind="scalar", expression="{a} * {b}", bindings={"a": "PV:A", "b": "PV:B"})
    gate_armed = False
    first_blocked = asyncio.Event()
    release_first = asyncio.Event()

    async def first_send(msg: dict) -> None:
        if gate_armed and msg["type"] == "result":
            first_blocked.set()
            await release_first.wait()

    moving, moving_send = _make_collector()
    await manager.add_subscription("c0", "s0", _scalar_spec(), t0_ns=0, t1_ns=0, send_fn=first_send)
    await manager.add_subscription("c1", "s1", _scalar_spec(), t0_ns=0, t1_ns=0, send_fn=moving_send)
    await asyncio.sleep(0.05)
    moving.clear()

    gate_armed = True
    fake_data_source.latest["PV:A"] = fake_data_source.latest["PV:A"].model_copy(update={"ts_ns": 5000, "value": 10.0})
    notifier.notify("PV:A")
    await asyncio.wait_for(first_blocked.wait(), 1.0)

    await manager.update_subscription("c1", "s1", moving_send, spec=product)
    await asyncio.sleep(0.05)
    release_first.set()
    await asyncio.sleep(0.05)

    assert _scalar_values(moving) == [30.0], "the sum's fan-out reached the subscription after it moved to the product"
    await manager.shutdown()


async def test_update_onto_a_running_eval_receives_a_result(manager):
    """A subscription updated onto an already-running push-mode eval gets a result.

    Two subscriptions share formula X; a third on formula Y is updated to X
    and receives X's current value without any PV change, while the existing
    listeners on X receive no duplicate.
    """
    first, first_send = _make_collector()
    second, second_send = _make_collector()
    await manager.add_subscription("c1", "s1", _scalar_spec(), t0_ns=0, t1_ns=0, send_fn=first_send)
    await manager.add_subscription("c2", "s2", _scalar_spec(), t0_ns=0, t1_ns=0, send_fn=second_send)
    product = FormulaSpec(kind="scalar", expression="{a} * {b}", bindings={"a": "PV:A", "b": "PV:B"})
    moving, moving_send = _make_collector()
    await manager.add_subscription("c3", "s3", product, t0_ns=0, t1_ns=0, send_fn=moving_send)
    await asyncio.sleep(0.05)
    assert _scalar_values(moving) == [15.0]

    ok = await manager.update_subscription("c3", "s3", moving_send, spec=_scalar_spec())
    await asyncio.sleep(0.05)

    assert ok is True
    assert _scalar_values(moving) == [15.0, 8.0], "updated subscription never got a result from the joined eval"
    assert _scalar_values(first) == [8.0]
    assert _scalar_values(second) == [8.0]
    await manager.shutdown()


async def test_catch_up_non_retriable_error_goes_to_the_joiner_only(manager):
    """A non-retriable `CanopyError` in the catch-up becomes an error frame for the joiner alone.

    The frame carries the error's code and detail, and the listener already on
    the shared eval receives nothing new.
    """
    first, first_send = _make_collector()
    await manager.add_subscription("c1", "s1", _scalar_spec(), t0_ns=0, t1_ns=0, send_fn=first_send)
    await asyncio.sleep(0.05)
    first_before = list(first)

    async def type_error(*args, **kwargs):
        raise FormulaTypeError("bad operand")

    manager._formula_service.evaluate = type_error

    late, late_send = _make_collector()
    await manager.add_subscription("c2", "s2", _scalar_spec(), t0_ns=0, t1_ns=0, send_fn=late_send)
    await asyncio.sleep(0.05)

    assert late == [{"type": "error", "id": "s2", "code": ErrorCode.FORMULA_TYPE_ERROR.value, "message": "bad operand"}]
    assert first == first_before
    await manager.shutdown()


async def test_catch_up_unhandled_exception_sends_unhandled_error(manager):
    """A generic exception in the catch-up becomes an ``UNHANDLED_EXCEPTION`` frame for the joiner."""
    first, first_send = _make_collector()
    await manager.add_subscription("c1", "s1", _scalar_spec(), t0_ns=0, t1_ns=0, send_fn=first_send)
    await asyncio.sleep(0.05)
    first_before = list(first)

    async def explode(*args, **kwargs):
        raise RuntimeError("kaboom")

    manager._formula_service.evaluate = explode

    late, late_send = _make_collector()
    await manager.add_subscription("c2", "s2", _scalar_spec(), t0_ns=0, t1_ns=0, send_fn=late_send)
    await asyncio.sleep(0.05)

    assert late == [{"type": "error", "id": "s2", "code": ErrorCode.UNHANDLED_EXCEPTION.value, "message": "kaboom"}]
    assert first == first_before
    await manager.shutdown()


async def test_catch_up_send_failure_drops_only_the_joiner(manager):
    """A failed catch-up send removes the joiner and keeps the shared eval for the others.

    The joiner leaves both the shared eval's listeners and the client
    subscription map; the eval stays registered because the first listener
    remains.
    """
    _, first_send = _make_collector()
    await manager.add_subscription("c1", "s1", _scalar_spec(), t0_ns=0, t1_ns=0, send_fn=first_send)
    await asyncio.sleep(0.05)
    key = manager._client_subs["c1"]["s1"]

    async def dead_send(_msg: dict) -> None:
        raise RuntimeError("socket closed")

    await manager.add_subscription("c2", "s2", _scalar_spec(), t0_ns=0, t1_ns=0, send_fn=dead_send)
    await asyncio.sleep(0.05)

    shared = manager._shared_evals[key]
    assert [(ls.client_id, ls.sub_id) for ls in shared.listeners] == [("c1", "s1")]
    assert "c2" not in manager._client_subs
    await manager.shutdown()


async def test_poll_mode_join_schedules_no_catch_up(manager):
    """Joining a running poll-mode eval schedules no catch-up evaluation.

    The next poll tick reaches every listener, so the joiner receives nothing
    before that tick.
    """
    _, first_send = _make_collector()
    await manager.add_subscription("c1", "s1", _scalar_spec(), t0_ns=0, t1_ns=0, send_fn=first_send, interval_ms=500)
    await asyncio.sleep(0.02)

    late, late_send = _make_collector()
    await manager.add_subscription("c2", "s2", _scalar_spec(), t0_ns=0, t1_ns=0, send_fn=late_send, interval_ms=500)
    shared = manager._shared_evals[manager._client_subs["c2"]["s2"]]
    assert shared.catch_up_tasks == set()

    await asyncio.sleep(0.05)
    assert late == []
    await manager.shutdown()
