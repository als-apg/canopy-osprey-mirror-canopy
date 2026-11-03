"""Shared-eval late joiners and client-visible input waits.

Subscriptions with identical parameters share one eval loop. A subscriber that
joins an existing loop must get the latest known outcome straight away: inputs
such as a ``*:SupplyOn`` PV may never change again, so waiting for the next
re-evaluation would leave the joiner without a result for good. While a loop
waits for inputs that have no data yet, the client is told so with a retryable
error instead of hearing nothing.

The joiner is replayed the eval's latest outcome (result, error or wait
notice); it never runs an evaluation of its own, so the eval rate stays that of
one loop however many listeners join, and no late-join work outlives the eval.
"""

import asyncio

import pytest

from tests.conftest import wait_for
from canopy.core.errors import ErrorCode
from canopy.contracts.sample import Sample, AlarmSeverity
from canopy.contracts.formula import FormulaSpec
from canopy.mole.adapters.base import ArchiverUnavailable
from canopy.formula_engine.config import FormulaEngineConfig
from canopy.formula_engine.errors import FormulaTypeError, FormulaInputMissing
from canopy.vole.services.notifier import ChangeNotifier
from tests.canopy.formula_engine.conftest import FakeDataSource
from canopy.formula_engine.services.parser import Parser
from canopy.formula_engine.services.formula_service import FormulaService
from canopy.formula_engine.services.subscription_manager import SubscriptionManager


def _sample(value: float) -> Sample:
    return Sample(ts_ns=1000, value=value, severity=AlarmSeverity.NO_ALARM, source="archive")


@pytest.fixture
def data_source() -> FakeDataSource:
    return FakeDataSource(latest={"PV:A": _sample(5.0), "PV:B": _sample(3.0)})


@pytest.fixture
def notifier() -> ChangeNotifier:
    return ChangeNotifier()


def _manager(data_source: FakeDataSource, notifier: ChangeNotifier, **kwargs) -> SubscriptionManager:
    async def subscribe_live(pvs: list[str]) -> None:
        return None

    return SubscriptionManager(
        formula_service=FormulaService(data_source, Parser(), FormulaEngineConfig()),
        on_change_fn=notifier.subscribe,
        subscribe_live_fn=subscribe_live,
        **kwargs,
    )


@pytest.fixture
def manager(data_source, notifier) -> SubscriptionManager:
    return _manager(data_source, notifier)


def _spec(b: str = "PV:B") -> FormulaSpec:
    return FormulaSpec(kind="scalar", expression="{a} + {b}", bindings={"a": "PV:A", "b": b})


class _Collector:
    def __init__(self) -> None:
        self.messages: list[dict] = []

    async def send(self, msg: dict) -> None:
        self.messages.append(msg)

    def of_type(self, kind: str) -> list[dict]:
        return [m for m in self.messages if m["type"] == kind]


def _make_collector() -> tuple[list[dict], "asyncio.Callable"]:
    messages: list[dict] = []

    async def send_fn(msg: dict) -> None:
        messages.append(msg)

    return messages, send_fn


async def _wait_for(predicate, timeout: float = 2.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.01)


async def test_two_subscribers_to_same_expression_both_get_a_result(data_source, notifier):
    mgr = _manager(data_source, notifier)
    first, second = _Collector(), _Collector()

    await mgr.add_subscription("c1", "s1", _spec(), t0_ns=0, t1_ns=0, send_fn=first.send)
    await mgr.add_subscription("c2", "s2", _spec(), t0_ns=0, t1_ns=0, send_fn=second.send)
    # Both share one eval and no input ever changes.
    assert len(mgr._shared_evals) == 1

    await _wait_for(lambda: first.of_type("result") and second.of_type("result"))
    assert first.of_type("result")[0]["id"] == "s1"
    assert second.of_type("result")[0]["id"] == "s2"
    assert second.of_type("result")[0]["data"]["scalar"]["value"] == pytest.approx(8.0)
    await mgr.shutdown()


async def test_joiner_after_first_result_gets_it_without_any_pv_change(data_source, notifier):
    mgr = _manager(data_source, notifier)
    first = _Collector()
    await mgr.add_subscription("c1", "s1", _spec(), t0_ns=0, t1_ns=0, send_fn=first.send)
    await _wait_for(lambda: first.of_type("result"))
    # Let the eval loop go idle: from here on nothing re-evaluates.
    await asyncio.sleep(0.05)

    joiner = _Collector()
    await mgr.add_subscription("c2", "s2", _spec(), t0_ns=0, t1_ns=0, send_fn=joiner.send)

    # Delivered during the join itself, with no PV notification.
    results = joiner.of_type("result")
    assert len(results) == 1
    assert results[0]["id"] == "s2"
    assert results[0]["data"]["scalar"]["value"] == pytest.approx(8.0)
    # The first subscriber is not re-sent anything because of the join.
    assert len(first.of_type("result")) == 1
    await mgr.shutdown()


async def test_same_client_duplicate_rule_gets_the_cached_result(data_source, notifier):
    # Two widgets on one screen with the same rule: same client, two sub ids.
    mgr = _manager(data_source, notifier)
    screen = _Collector()
    await mgr.add_subscription("c1", "s1", _spec(), t0_ns=0, t1_ns=0, send_fn=screen.send)
    await _wait_for(lambda: screen.of_type("result"))
    await asyncio.sleep(0.05)
    await mgr.add_subscription("c1", "s2", _spec(), t0_ns=0, t1_ns=0, send_fn=screen.send)

    assert {m["id"] for m in screen.of_type("result")} == {"s1", "s2"}
    await mgr.shutdown()


async def test_missing_input_notifies_the_client_with_a_retryable_error(data_source, notifier):
    mgr = _manager(data_source, notifier, input_wait_notify_s=0.0)
    client = _Collector()
    await mgr.add_subscription("c1", "s1", _spec("PV:MISSING"), t0_ns=0, t1_ns=0, send_fn=client.send)

    await _wait_for(lambda: client.of_type("error"))
    err = client.of_type("error")[0]
    assert err["id"] == "s1"
    assert err["code"] == "formula_input_missing"
    assert err["retryable"] is True
    assert err["message"]
    await mgr.shutdown()


async def test_retryable_error_is_sent_once_not_on_every_retry(data_source, notifier):
    mgr = _manager(data_source, notifier, input_wait_notify_s=0.0)
    client = _Collector()
    await mgr.add_subscription("c1", "s1", _spec("PV:MISSING"), t0_ns=0, t1_ns=0, send_fn=client.send)

    # Several 250 ms retry periods.
    await asyncio.sleep(0.9)
    assert len(client.of_type("error")) == 1
    await mgr.shutdown()


async def test_retry_continues_server_side_and_delivers_once_input_arrives(data_source, notifier):
    mgr = _manager(data_source, notifier, input_wait_notify_s=0.0)
    client = _Collector()
    await mgr.add_subscription("c1", "s1", _spec("PV:LATE"), t0_ns=0, t1_ns=0, send_fn=client.send)
    await _wait_for(lambda: client.of_type("error"))

    # The input gets data, but no change notification is fired.
    data_source.latest["PV:LATE"] = _sample(1.0)

    await _wait_for(lambda: client.of_type("result"))
    assert client.of_type("result")[0]["data"]["scalar"]["value"] == pytest.approx(6.0)
    await mgr.shutdown()


async def test_short_input_wait_stays_silent(data_source, notifier):
    # An input that shows up within the notify threshold (the normal case on a
    # fresh subscribe, before the first live value lands) must not produce an
    # error frame per rule.
    mgr = _manager(data_source, notifier, input_wait_notify_s=5.0)
    client = _Collector()
    await mgr.add_subscription("c1", "s1", _spec("PV:LATE"), t0_ns=0, t1_ns=0, send_fn=client.send)
    await asyncio.sleep(0.3)
    data_source.latest["PV:LATE"] = _sample(1.0)

    await _wait_for(lambda: client.of_type("result"))
    assert client.of_type("error") == []
    await mgr.shutdown()


async def test_joiner_of_a_waiting_eval_gets_the_retryable_status(data_source, notifier):
    mgr = _manager(data_source, notifier, input_wait_notify_s=0.0)
    first = _Collector()
    await mgr.add_subscription("c1", "s1", _spec("PV:MISSING"), t0_ns=0, t1_ns=0, send_fn=first.send)
    await _wait_for(lambda: first.of_type("error"))

    joiner = _Collector()
    await mgr.add_subscription("c2", "s2", _spec("PV:MISSING"), t0_ns=0, t1_ns=0, send_fn=joiner.send)

    errors = joiner.of_type("error")
    assert len(errors) == 1
    assert errors[0]["id"] == "s2"
    assert errors[0]["retryable"] is True
    await mgr.shutdown()


async def test_push_mode_retries_every_code_it_flags_retryable(data_source, notifier):
    """An archiver outage is flagged ``retryable`` and is retried, with no input change to wake the loop."""
    mgr = _manager(data_source, notifier, input_wait_notify_s=0.0)
    real_evaluate = mgr._formula_service.evaluate
    failures = 2

    async def flaky_archiver(*args, **kwargs):
        nonlocal failures
        if failures:
            failures -= 1
            raise ArchiverUnavailable("archiver down")
        return await real_evaluate(*args, **kwargs)

    mgr._formula_service.evaluate = flaky_archiver
    client = _Collector()
    await mgr.add_subscription("c1", "s1", _spec(), t0_ns=0, t1_ns=0, send_fn=client.send)

    await _wait_for(lambda: client.of_type("result"), timeout=3.0)
    errors = client.of_type("error")
    assert [(e["code"], e["retryable"]) for e in errors] == [(ErrorCode.ARCHIVER_UNAVAILABLE.value, True)]
    assert client.of_type("result")[0]["data"]["scalar"]["value"] == pytest.approx(8.0)
    await mgr.shutdown()


async def test_push_mode_does_not_retry_a_final_error(data_source, notifier):
    mgr = _manager(data_source, notifier, input_wait_notify_s=0.0)
    calls = 0

    async def type_error(*args, **kwargs):
        nonlocal calls
        calls += 1
        raise FormulaTypeError("bad operand")

    mgr._formula_service.evaluate = type_error
    client = _Collector()
    await mgr.add_subscription("c1", "s1", _spec(), t0_ns=0, t1_ns=0, send_fn=client.send)
    await _wait_for(lambda: client.of_type("error"))
    await asyncio.sleep(0.6)

    assert calls == 1
    assert [e["retryable"] for e in client.of_type("error")] == [False]
    await mgr.shutdown()


async def test_push_mode_retries_an_unhandled_exception_without_repeating_its_frame(data_source, notifier):
    mgr = _manager(data_source, notifier)
    calls = 0

    async def explode(*args, **kwargs):
        nonlocal calls
        calls += 1
        raise RuntimeError("kaboom")

    mgr._formula_service.evaluate = explode
    client = _Collector()
    await mgr.add_subscription("c1", "s1", _spec(), t0_ns=0, t1_ns=0, send_fn=client.send)
    await _wait_for(lambda: calls >= 3, timeout=3.0)

    errors = client.of_type("error")
    assert [(e["code"], e["retryable"]) for e in errors] == [(ErrorCode.UNHANDLED_EXCEPTION.value, True)]
    await mgr.shutdown()


async def test_poll_mode_missing_input_notifies_the_client(data_source, notifier):
    mgr = _manager(data_source, notifier, input_wait_notify_s=0.0)
    client = _Collector()
    await mgr.add_subscription("c1", "s1", _spec("PV:MISSING"), t0_ns=0, t1_ns=0, send_fn=client.send, interval_ms=50)
    await asyncio.sleep(0.4)

    errors = client.of_type("error")
    assert len(errors) == 1
    assert errors[0]["retryable"] is True
    await mgr.shutdown()


async def test_poll_mode_joiner_gets_the_cached_result(data_source, notifier):
    mgr = _manager(data_source, notifier)
    first = _Collector()
    await mgr.add_subscription("c1", "s1", _spec(), t0_ns=0, t1_ns=0, send_fn=first.send, interval_ms=10_000)
    await _wait_for(lambda: first.of_type("result"))

    joiner = _Collector()
    await mgr.add_subscription("c2", "s2", _spec(), t0_ns=0, t1_ns=0, send_fn=joiner.send, interval_ms=10_000)
    # Replayed during the join, long before the next tick, and by no second eval.
    assert len(joiner.of_type("result")) == 1
    assert len(mgr._shared_evals) == 1
    await mgr.shutdown()


# --- replay ordering, shutdown and failures ----------------------------- #


async def test_shutdown_leaves_no_late_join_work_running(manager):
    """A late join starts no evaluation of its own, so nothing reaches the joiner after ``shutdown``.

    The joiner is replayed the eval's latest outcome during the join, so a
    slow evaluator is never entered by the join and no late-join work
    outlives ``shutdown``.
    """
    first, first_send = _make_collector()
    await manager.add_subscription("c1", "s1", _spec(), t0_ns=0, t1_ns=0, send_fn=first_send)
    assert await wait_for(lambda: first)

    gate = asyncio.Event()
    entered = asyncio.Event()
    real_evaluate = manager._formula_service.evaluate

    async def slow_evaluate(*args, **kwargs):
        entered.set()
        await gate.wait()
        return await real_evaluate(*args, **kwargs)

    manager._formula_service.evaluate = slow_evaluate

    late, late_send = _make_collector()
    await manager.add_subscription("c2", "s2", _spec(), t0_ns=0, t1_ns=0, send_fn=late_send)
    assert _scalar_values(late) == [8.0]
    assert not entered.is_set(), "the join evaluated instead of replaying"

    await manager.shutdown()
    gate.set()
    await asyncio.sleep(0.1)

    assert _scalar_values(late) == [8.0], "a frame reached the joiner after shutdown()"


async def test_shutdown_stops_the_shared_retries_while_a_joiner_is_attached(manager, notifier):
    """``shutdown`` stops the shared eval's retrying on missing inputs while a joiner is attached.

    A joiner never evaluates, so a PV change sends the shared eval itself
    into its retry loop. Shutdown leaves the listener list intact, so nothing
    but cancelling the shared eval's loop ends the retries.
    """
    first, first_send = _make_collector()
    await manager.add_subscription("c1", "s1", _spec(), t0_ns=0, t1_ns=0, send_fn=first_send)
    assert await wait_for(lambda: first)

    calls = 0

    async def always_retriable(*args, **kwargs):
        nonlocal calls
        calls += 1
        raise FormulaInputMissing("input not ready")

    manager._formula_service.evaluate = always_retriable

    _, late_send = _make_collector()
    await manager.add_subscription("c2", "s2", _spec(), t0_ns=0, t1_ns=0, send_fn=late_send)
    notifier.notify("PV:A")
    assert await wait_for(lambda: calls >= 2)

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


async def test_late_join_replay_does_not_overwrite_a_newer_fan_out(manager, notifier, data_source):
    """A replay still in flight when a fresher fan-out arrives is sent first, never after it.

    The joiner's send stalls on its replayed result while a PV change fans the fresh value out.
    The fan-out waits on the joiner's lock, so the joiner ends on the fresh
    value instead of having it overwritten by the older one.
    """
    first, first_send = _make_collector()
    await manager.add_subscription("c1", "s1", _spec(), t0_ns=0, t1_ns=0, send_fn=first_send)
    assert await wait_for(lambda: first)

    late: list[dict] = []
    replaying = asyncio.Event()
    release = asyncio.Event()

    async def late_send(msg: dict) -> None:
        late.append(msg)
        if len(late) == 1:
            replaying.set()
            await release.wait()

    join = asyncio.create_task(manager.add_subscription("c2", "s2", _spec(), t0_ns=0, t1_ns=0, send_fn=late_send))
    await asyncio.wait_for(replaying.wait(), 1.0)

    data_source.latest["PV:A"] = data_source.latest["PV:A"].model_copy(update={"ts_ns": 5000, "value": 10.0})
    notifier.notify("PV:A")
    assert await wait_for(lambda: _scalar_values(first) == [8.0, 13.0])

    release.set()
    await asyncio.wait_for(join, 1.0)
    assert await wait_for(lambda: len(_scalar_values(late)) == 2)

    assert _scalar_values(late) == [8.0, 13.0], "the replayed result arrived after the fresher fan-out"
    await manager.shutdown()


async def test_late_join_does_not_send_behind_an_in_flight_fan_out(manager, notifier, data_source):
    """A joiner that arrives while a fresher fan-out is mid-send is replayed that fresh frame, not a stale one.

    The first listener's send stalls on the fanned-out fresh value. The fan-out recorded
    that frame as the latest outcome before sending, so the joiner is replayed
    it, and the in-flight fan-out, whose snapshot predates the join, does not
    send it a second time.
    """
    first: list[dict] = []
    gate_armed = False
    first_blocked = asyncio.Event()
    release_first = asyncio.Event()

    async def first_send(msg: dict) -> None:
        first.append(msg)
        if gate_armed and msg["type"] == "result":
            first_blocked.set()
            await release_first.wait()

    await manager.add_subscription("c1", "s1", _spec(), t0_ns=0, t1_ns=0, send_fn=first_send)
    assert await wait_for(lambda: first)

    gate_armed = True
    data_source.latest["PV:A"] = data_source.latest["PV:A"].model_copy(update={"ts_ns": 5000, "value": 10.0})
    notifier.notify("PV:A")
    await asyncio.wait_for(first_blocked.wait(), 1.0)

    late, late_send = _make_collector()
    await manager.add_subscription("c2", "s2", _spec(), t0_ns=0, t1_ns=0, send_fn=late_send)
    assert _scalar_values(late) == [13.0]

    release_first.set()
    await asyncio.sleep(0.05)

    assert _scalar_values(late) == [13.0], "the joiner got a stale or duplicate frame around the in-flight fan-out"
    await manager.shutdown()


async def test_late_joiner_never_evaluates_the_shared_eval_retries_alone(manager, notifier, monkeypatch):
    """A joiner never evaluates: the shared eval's own loop is the only one retrying a missing input.

    The join replays the latest result without calling the evaluator, and once a PV change
    hits the missing input every evaluation comes from the shared eval's task.
    """
    monkeypatch.setattr("canopy.formula_engine.services.subscription_manager._INPUT_RETRY_DELAY_S", 0.01)
    first, first_send = _make_collector()
    await manager.add_subscription("c1", "s1", _spec(), t0_ns=0, t1_ns=0, send_fn=first_send)
    assert await wait_for(lambda: first)
    shared = manager._shared_evals[manager._client_subs["c1"]["s1"]]

    callers: list[asyncio.Task] = []

    async def missing(*args, **kwargs):
        callers.append(asyncio.current_task())
        raise FormulaInputMissing("input not ready")

    manager._formula_service.evaluate = missing

    late, late_send = _make_collector()
    await manager.add_subscription("c2", "s2", _spec(), t0_ns=0, t1_ns=0, send_fn=late_send)
    assert _scalar_values(late) == [8.0]
    assert callers == []

    notifier.notify("PV:A")
    assert await wait_for(lambda: callers.count(shared.task) > 1), "the shared eval did not do the retries"
    assert set(callers) == {shared.task}
    await manager.shutdown()


async def test_joiners_of_an_eval_without_output_share_its_first_result(manager, data_source, monkeypatch):
    """Joiners of an eval that has fanned out nothing get no replay and receive its first result.

    While an input is missing the shared eval retries alone, so the evaluation
    rate stays at that of a single loop however many listeners join. Once the
    input arrives, the retrying loop picks it up and every listener receives
    the same first result exactly once.
    """
    from canopy.contracts.sample import Sample, AlarmSeverity

    monkeypatch.setattr("canopy.formula_engine.services.subscription_manager._INPUT_RETRY_DELAY_S", 0.01)
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
    # Nothing has been fanned out yet, so no joiner was replayed anything.
    assert shared.last_frame is None
    assert not any(collected)

    calls = 0
    await asyncio.sleep(0.1)
    assert calls <= 20, f"{calls} evaluations in 0.1 s; expected one retry loop of about 10"

    data_source.latest["PV:C"] = Sample(ts_ns=3000, value=1.0, severity=AlarmSeverity.NO_ALARM, source="archive")
    assert await wait_for(lambda: all(_scalar_values(messages) for messages in collected))

    assert [_scalar_values(messages) for messages in collected] == [[6.0]] * 10
    await manager.shutdown()


async def test_resubscribe_of_a_late_joiner_delivers_one_result(manager, notifier, data_source):
    """Re-subscribing an id leaves only the new listener attached, even with the same client, id and send.

    Each subscription of the id is replayed the current result once, and a later fan-out
    reaches the client once, not also through the replaced listener, because
    listeners compare by identity.
    """
    first, first_send = _make_collector()
    await manager.add_subscription("c0", "s0", _spec(), t0_ns=0, t1_ns=0, send_fn=first_send)
    assert await wait_for(lambda: first)

    late, late_send = _make_collector()
    await manager.add_subscription("c1", "s1", _spec(), t0_ns=0, t1_ns=0, send_fn=late_send)
    await manager.add_subscription("c1", "s1", _spec(), t0_ns=0, t1_ns=0, send_fn=late_send)
    assert _scalar_values(late) == [8.0, 8.0]

    data_source.latest["PV:A"] = data_source.latest["PV:A"].model_copy(update={"ts_ns": 5000, "value": 10.0})
    notifier.notify("PV:A")
    assert await wait_for(lambda: len(_scalar_values(late)) == 3)
    await asyncio.sleep(0.05)

    assert _scalar_values(late) == [8.0, 8.0, 13.0], "the replaced listener still received the fan-out"
    await manager.shutdown()


async def test_update_onto_a_running_eval_receives_a_result(manager):
    """A subscription updated onto an already-running push-mode eval gets a result.

    Two subscriptions share formula X; a third on formula Y is updated to X
    and receives X's current value without any PV change, while the existing
    listeners on X receive no duplicate.
    """
    first, first_send = _make_collector()
    second, second_send = _make_collector()
    await manager.add_subscription("c1", "s1", _spec(), t0_ns=0, t1_ns=0, send_fn=first_send)
    await manager.add_subscription("c2", "s2", _spec(), t0_ns=0, t1_ns=0, send_fn=second_send)
    product = FormulaSpec(kind="scalar", expression="{a} * {b}", bindings={"a": "PV:A", "b": "PV:B"})
    moving, moving_send = _make_collector()
    await manager.add_subscription("c3", "s3", product, t0_ns=0, t1_ns=0, send_fn=moving_send)
    assert await wait_for(lambda: moving and first and second)
    assert _scalar_values(moving) == [15.0]

    ok = await manager.update_subscription("c3", "s3", moving_send, spec=_spec())
    assert await wait_for(lambda: len(moving) >= 2)

    assert ok is True
    assert _scalar_values(moving) == [15.0, 8.0], "updated subscription never got a result from the joined eval"
    assert _scalar_values(first) == [8.0]
    assert _scalar_values(second) == [8.0]
    await manager.shutdown()


async def test_late_join_replays_a_final_error_to_the_joiner_only(manager, notifier):
    """A joiner of an eval whose latest outcome is a non-retriable error is replayed that error alone.

    The error comes from the shared eval, not from an evaluation of the joiner's own; the frame
    carries the error's code and detail plus ``retryable: false``, and the
    listener already on the shared eval receives nothing new from the join.
    """
    first, first_send = _make_collector()
    await manager.add_subscription("c1", "s1", _spec(), t0_ns=0, t1_ns=0, send_fn=first_send)
    assert await wait_for(lambda: first)

    async def type_error(*args, **kwargs):
        raise FormulaTypeError("bad operand")

    manager._formula_service.evaluate = type_error
    notifier.notify("PV:A")
    assert await wait_for(lambda: first[-1]["type"] == "error")
    first_before = list(first)

    late, late_send = _make_collector()
    await manager.add_subscription("c2", "s2", _spec(), t0_ns=0, t1_ns=0, send_fn=late_send)

    assert late == [
        {
            "type": "error",
            "id": "s2",
            "code": ErrorCode.FORMULA_TYPE_ERROR.value,
            "message": "bad operand",
            "retryable": False,
        }
    ]
    assert first == first_before
    await manager.shutdown()


async def test_late_join_replays_an_unhandled_exception_frame(manager, notifier):
    """A joiner of an eval whose latest outcome is an unhandled exception is replayed that ``UNHANDLED_EXCEPTION`` frame.

    The frame is flagged ``retryable`` as every live unhandled-exception
    frame is.
    """
    first, first_send = _make_collector()
    await manager.add_subscription("c1", "s1", _spec(), t0_ns=0, t1_ns=0, send_fn=first_send)
    assert await wait_for(lambda: first)

    async def explode(*args, **kwargs):
        raise RuntimeError("kaboom")

    manager._formula_service.evaluate = explode
    notifier.notify("PV:A")
    assert await wait_for(lambda: first[-1]["type"] == "error")
    first_before = list(first)

    late, late_send = _make_collector()
    await manager.add_subscription("c2", "s2", _spec(), t0_ns=0, t1_ns=0, send_fn=late_send)

    assert late == [
        {
            "type": "error",
            "id": "s2",
            "code": ErrorCode.UNHANDLED_EXCEPTION.value,
            "message": "kaboom",
            "retryable": True,
        }
    ]
    assert first == first_before
    await manager.shutdown()


async def test_late_join_send_failure_drops_only_the_joiner(manager):
    """A failed replay send removes the joiner and keeps the shared eval for the others.

    The joiner leaves both the shared eval's listeners and the client
    subscription map; the eval stays registered because the first listener
    remains.
    """
    first, first_send = _make_collector()
    await manager.add_subscription("c1", "s1", _spec(), t0_ns=0, t1_ns=0, send_fn=first_send)
    assert await wait_for(lambda: first)
    key = manager._client_subs["c1"]["s1"]

    async def dead_send(_msg: dict) -> None:
        raise RuntimeError("socket closed")

    await manager.add_subscription("c2", "s2", _spec(), t0_ns=0, t1_ns=0, send_fn=dead_send)
    assert await wait_for(lambda: "c2" not in manager._client_subs)

    shared = manager._shared_evals[key]
    assert [(ls.client_id, ls.sub_id) for ls in shared.listeners] == [("c1", "s1")]
    assert "c2" not in manager._client_subs
    await manager.shutdown()
