"""Gateway: bind/get lifecycle and the public facades route to the service."""

import pytest

from canopy.raccoon import gateway
from canopy.raccoon.cache.keys import NameKind


@pytest.mark.asyncio
async def test_get_service_raises_when_unbound() -> None:
    gateway.bind(None)
    with pytest.raises(RuntimeError):
        gateway.get_service()


@pytest.mark.asyncio
async def test_facades_delegate_to_bound_service(directory_service, fake_adapter) -> None:
    fake_adapter.pvs = {"P:1", "P:2"}
    fake_adapter.devices = {"D:1"}
    fake_adapter.elements = {"E:1"}

    gateway.bind(directory_service)
    try:
        assert await gateway.list_pvs("P") == ["P:1", "P:2"]
        assert await gateway.list_devices("D") == ["D:1"]
        assert await gateway.list_elements("E") == ["E:1"]
        assert await gateway.exists("P:1") is True
        assert await gateway.exists("P:9") is False

        result = await gateway.validate(["P:1", "P:9"])
        assert result.known == ["P:1"]
        assert result.unknown == ["P:9"]

        stats = await gateway.stats()
        assert stats.adapter_name == "fake"
    finally:
        gateway.bind(None)


@pytest.mark.asyncio
async def test_list_calls_use_correct_kind_through_facade(directory_service, fake_adapter) -> None:
    fake_adapter.pvs = {"X"}
    gateway.bind(directory_service)
    try:
        await gateway.list_pvs("X")
    finally:
        gateway.bind(None)
    kinds = [c[0] for c in fake_adapter.calls]
    assert "list_pvs" in kinds
    assert NameKind.PV.value == "pv"  # Sanity-check the enum value.
