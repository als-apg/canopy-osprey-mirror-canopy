"""Tests for the Formula Engine's SubscriptionManager live subscription orchestrator."""

import asyncio

import pytest

from tests.conftest import wait_for
from canopy.contracts.formula import FormulaSpec
from canopy.formula_engine.config import FormulaEngineConfig
from canopy.formula_engine.errors import SubscriptionLimitExceeded
from canopy.vole.services.notifier import ChangeNotifier
from tests.canopy.formula_engine.conftest import FakeDataSource
from canopy.formula_engine.services.parser import Parser
from canopy.formula_engine.services.formula_service import FormulaService
from canopy.formula_engine.services.subscription_manager import SubscriptionManager


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
    return FormulaService(fake_data_source, Parser(), FormulaEngineConfig())


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

        assert await wait_for(lambda: len(messages) >= 1)
        assert messages[0]["type"] == "result"
        assert messages[0]["id"] == "sub-1"
        assert "data" in messages[0]

        await manager.shutdown()

    async def test_notification_triggers_reevaluation(self, manager, notifier):
        messages, send_fn = _make_collector()

        await manager.add_subscription("client-1", "sub-1", _scalar_spec(), 0, 0, send_fn)
        assert await wait_for(lambda: len(messages) >= 1)

        initial_count = len(messages)
        notifier.notify("PV:A")

        assert await wait_for(lambda: len(messages) > initial_count)
        assert messages[-1]["type"] == "result"

        await manager.shutdown()

    async def test_coalescing_rapid_notifications(self, manager, notifier):
        messages, send_fn = _make_collector()

        await manager.add_subscription("client-1", "sub-1", _timeseries_spec(), 1000, 0, send_fn)
        assert await wait_for(lambda: len(messages) >= 1)
        initial_count = len(messages)

        for _ in range(10):
            notifier.notify("PV:A")

        def new_results() -> list[dict]:
            return [m for m in messages[initial_count:] if m["type"] == "result"]

        assert await wait_for(lambda: len(new_results()) >= 1)
        await asyncio.sleep(0.1)  # settle; a slow runner only lowers the count, so this can't flake
        assert len(new_results()) <= 3

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

        assert await wait_for(lambda: len(messages) >= 1)
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

        assert await wait_for(lambda: len([m for m in messages if m["type"] == "result"]) >= 3)

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
        assert await wait_for(lambda: messages_1 and messages_2)

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


async def test_all_dead_listeners_retire_the_shared_eval(manager, notifier):
    """A shared eval whose every result send fails is torn down and unregistered.

    The retired eval is absent from the deduplication map, so a fresh
    subscriber with the same parameters starts a working eval instead of
    joining a loop whose clients are all gone.
    """

    async def dead_send(_msg: dict) -> None:
        raise RuntimeError("socket closed")

    await manager.add_subscription("c1", "s1", _scalar_spec(), t0_ns=0, t1_ns=0, send_fn=dead_send)
    assert await wait_for(lambda: manager._shared_evals == {})
    assert manager._client_subs.get("c1", {}) == {}

    fresh, fresh_send = _make_collector()
    await manager.add_subscription("c2", "s2", _scalar_spec(), t0_ns=0, t1_ns=0, send_fn=fresh_send)
    assert await wait_for(lambda: any(m["type"] == "result" for m in fresh))

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
    assert await wait_for(lambda: manager._shared_evals == {})

    assert manager._client_subs.get("c1", {}) == {}

    await manager.shutdown()


def _scalar_values(messages: list[dict]) -> list:
    """``data.scalar.value`` of every result frame, in arrival order."""
    return [m["data"]["scalar"]["value"] for m in messages if m["type"] == "result"]


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
    assert await wait_for(lambda: moving)
    moving.clear()

    gate_armed = True
    fake_data_source.latest["PV:A"] = fake_data_source.latest["PV:A"].model_copy(update={"ts_ns": 5000, "value": 10.0})
    notifier.notify("PV:A")
    await asyncio.wait_for(first_blocked.wait(), 1.0)

    await manager.update_subscription("c1", "s1", moving_send, spec=product)
    assert await wait_for(lambda: moving)
    release_first.set()
    await asyncio.sleep(0.05)

    assert _scalar_values(moving) == [30.0], "the sum's fan-out reached the subscription after it moved to the product"
    await manager.shutdown()
