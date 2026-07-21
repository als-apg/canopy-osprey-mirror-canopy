"""Tests for PVBroker activation/deactivation lifecycle hooks."""

from tests.canopy.hare.conftest import FakeWebSocket
from canopy.hare.services.broker import PVBroker


class TestActivationHooks:
    async def test_activation_hook_fires_on_first_subscriber(self, broker: PVBroker) -> None:
        activated: list[list[str]] = []

        async def hook(pvs: list[str]) -> None:
            activated.append(pvs)

        broker.add_activation_hook(hook)
        broker.add_client("c1", FakeWebSocket())
        await broker.subscribe("c1", ["PV:A", "PV:B"])

        assert activated == [["PV:A", "PV:B"]]

    async def test_activation_hook_not_fired_for_already_active_pv(self, broker: PVBroker) -> None:
        activated: list[list[str]] = []

        async def hook(pvs: list[str]) -> None:
            activated.append(pvs)

        broker.add_activation_hook(hook)
        broker.add_client("c1", FakeWebSocket())
        broker.add_client("c2", FakeWebSocket())
        await broker.subscribe("c1", ["PV:A"])
        await broker.subscribe("c2", ["PV:A"])

        assert activated == [["PV:A"]]

    async def test_activation_hook_excluded_client(self, broker: PVBroker) -> None:
        activated: list[list[str]] = []

        async def hook(pvs: list[str]) -> None:
            activated.append(pvs)

        broker.add_activation_hook(hook, exclude_clients={"c1"})
        broker.add_client("c1", FakeWebSocket())
        await broker.subscribe("c1", ["PV:A"])

        assert activated == []

    async def test_remove_activation_hook(self, broker: PVBroker) -> None:
        activated: list[list[str]] = []

        async def hook(pvs: list[str]) -> None:
            activated.append(pvs)

        broker.add_activation_hook(hook)
        broker.remove_activation_hook(hook)
        broker.add_client("c1", FakeWebSocket())
        await broker.subscribe("c1", ["PV:A"])

        assert activated == []


class TestDeactivationHooks:
    async def test_deactivation_hook_fires_on_last_unsubscribe(self, broker: PVBroker) -> None:
        deactivated: list[list[str]] = []

        async def hook(pvs: list[str]) -> None:
            deactivated.append(pvs)

        broker.add_deactivation_hook(hook)
        broker.add_client("c1", FakeWebSocket())
        await broker.subscribe("c1", ["PV:A"])
        await broker.unsubscribe("c1", ["PV:A"])

        assert deactivated == [["PV:A"]]

    async def test_deactivation_hook_fires_on_remove_client(self, broker: PVBroker) -> None:
        deactivated: list[list[str]] = []

        async def hook(pvs: list[str]) -> None:
            deactivated.append(pvs)

        broker.add_deactivation_hook(hook)
        broker.add_client("c1", FakeWebSocket())
        await broker.subscribe("c1", ["PV:A", "PV:B"])
        await broker.remove_client("c1")

        assert len(deactivated) == 1
        assert set(deactivated[0]) == {"PV:A", "PV:B"}

    async def test_deactivation_not_fired_when_other_subscribers_remain(self, broker: PVBroker) -> None:
        deactivated: list[list[str]] = []

        async def hook(pvs: list[str]) -> None:
            deactivated.append(pvs)

        broker.add_deactivation_hook(hook)
        broker.add_client("c1", FakeWebSocket())
        broker.add_client("c2", FakeWebSocket())
        await broker.subscribe("c1", ["PV:A"])
        await broker.subscribe("c2", ["PV:A"])
        await broker.remove_client("c1")

        assert deactivated == []

    async def test_deactivation_hook_excluded_client(self, broker: PVBroker) -> None:
        deactivated: list[list[str]] = []

        async def hook(pvs: list[str]) -> None:
            deactivated.append(pvs)

        broker.add_deactivation_hook(hook, exclude_clients={"c1"})
        broker.add_client("c1", FakeWebSocket())
        await broker.subscribe("c1", ["PV:A"])
        await broker.remove_client("c1")

        assert deactivated == []

    async def test_remove_deactivation_hook(self, broker: PVBroker) -> None:
        deactivated: list[list[str]] = []

        async def hook(pvs: list[str]) -> None:
            deactivated.append(pvs)

        broker.add_deactivation_hook(hook)
        broker.remove_deactivation_hook(hook)
        broker.add_client("c1", FakeWebSocket())
        await broker.subscribe("c1", ["PV:A"])
        await broker.remove_client("c1")

        assert deactivated == []
