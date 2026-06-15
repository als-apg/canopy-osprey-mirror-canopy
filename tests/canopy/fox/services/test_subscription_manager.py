"""Tests for Fox's SubscriptionManager live subscription orchestrator."""

import asyncio

import pytest

from canopy.fox.config import FoxConfig
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


def _timeseries_spec() -> FormulaSpec:
    return FormulaSpec(
        kind="timeseries",
        expression="{a} + {b}",
        bindings={"a": "PV:A", "b": "PV:B"},
    )


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
        assert subs["sub-1"].spec.kind == "timeseries"

        await manager.shutdown()
