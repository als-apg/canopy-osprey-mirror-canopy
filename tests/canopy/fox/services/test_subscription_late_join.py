"""Shared-eval late joiners and client-visible input waits.

Subscriptions with identical parameters share one eval loop. A subscriber that
joins an existing loop must get the latest known outcome straight away: inputs
such as a ``*:SupplyOn`` PV may never change again, so waiting for the next
re-evaluation would leave the joiner without a result for good. While a loop
waits for inputs that have no data yet, the client is told so with a retryable
error instead of hearing nothing.
"""

import asyncio

import pytest

from canopy.fox.config import FoxConfig
from canopy.contracts.sample import Sample, AlarmSeverity
from canopy.contracts.formula import FormulaSpec
from tests.canopy.fox.conftest import FakeDataSource
from canopy.fox.services.parser import Parser
from canopy.vole.services.notifier import ChangeNotifier
from canopy.fox.services.formula_service import FormulaService
from canopy.fox.services.subscription_manager import SubscriptionManager


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
        formula_service=FormulaService(data_source, Parser(), FoxConfig()),
        on_change_fn=notifier.subscribe,
        subscribe_live_fn=subscribe_live,
        **kwargs,
    )


def _spec(b: str = "PV:B") -> FormulaSpec:
    return FormulaSpec(kind="scalar", expression="{a} + {b}", bindings={"a": "PV:A", "b": b})


class _Collector:
    def __init__(self) -> None:
        self.messages: list[dict] = []

    async def send(self, msg: dict) -> None:
        self.messages.append(msg)

    def of_type(self, kind: str) -> list[dict]:
        return [m for m in self.messages if m["type"] == kind]


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


async def test_update_onto_an_existing_eval_gets_its_cached_result(data_source, notifier):
    mgr = _manager(data_source, notifier)
    a, b = _Collector(), _Collector()
    await mgr.add_subscription("c1", "s1", _spec(), t0_ns=0, t1_ns=0, send_fn=a.send)
    await mgr.add_subscription("c2", "s2", _spec("PV:A"), t0_ns=0, t1_ns=0, send_fn=b.send)
    await _wait_for(lambda: a.of_type("result") and b.of_type("result"))
    await asyncio.sleep(0.05)
    before = len(b.of_type("result"))

    # s2 switches to s1's spec: it joins s1's idle eval and must get its value.
    assert await mgr.update_subscription("c2", "s2", b.send, spec=_spec())
    results = b.of_type("result")
    assert len(results) == before + 1
    assert results[-1]["data"]["scalar"]["value"] == pytest.approx(8.0)
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
    assert len(joiner.of_type("result")) == 1
    await mgr.shutdown()


async def test_join_replay_survives_a_dead_joiner_socket(data_source, notifier):
    mgr = _manager(data_source, notifier)
    first = _Collector()
    await mgr.add_subscription("c1", "s1", _spec(), t0_ns=0, t1_ns=0, send_fn=first.send)
    await _wait_for(lambda: first.of_type("result"))

    async def dead(msg: dict) -> None:
        raise RuntimeError("socket closed")

    # A send failure while replaying must not fail the subscribe call.
    await mgr.add_subscription("c2", "s2", _spec(), t0_ns=0, t1_ns=0, send_fn=dead)
    await mgr.shutdown()
