"""End-to-end: registry resolves an out-of-tree adapter via `module:Class`.

The Raccoon analog of Mole's dotted-path test. Verifies that a config string
like `tests._fixtures.raccoon_example_adapter:ExampleDirectoryAdapter` loads
the class, builds an instance, and the resulting registry serves real
gateway calls.
"""

import pytest

from canopy.raccoon import gateway
from canopy.raccoon.config import CacheConfig, RaccoonConfig, DirectoryInstanceConfig
from canopy.raccoon.cache.flight import SingleFlight
from canopy.raccoon.adapters.base import AdapterMisconfigured
from canopy.raccoon.cache.name_cache import PVNameCache
from canopy.raccoon.adapters.registry import build_registry
from canopy.raccoon.cache.pattern_cache import PatternCache
from tests._fixtures.raccoon_example_adapter import ExampleDirectoryAdapter
from canopy.raccoon.services.directory_service import DirectoryService

_DOTTED_PATH = "tests._fixtures.raccoon_example_adapter:ExampleDirectoryAdapter"


def _config_with_type(type_str: str) -> RaccoonConfig:
    return RaccoonConfig(
        directory=DirectoryInstanceConfig(type=type_str, cache=CacheConfig()),
    )


def test_registry_loads_dotted_path_adapter() -> None:
    cfg = _config_with_type(_DOTTED_PATH)
    registry = build_registry(cfg)
    adapter = registry.require()
    assert isinstance(adapter, ExampleDirectoryAdapter)


def test_unknown_short_name_raises_adapter_misconfigured() -> None:
    cfg = _config_with_type("definitely-not-registered")
    with pytest.raises(AdapterMisconfigured):
        build_registry(cfg)


def test_dotted_path_with_missing_module_raises_adapter_misconfigured() -> None:
    cfg = _config_with_type("nonexistent.package:NotAClass")
    with pytest.raises(AdapterMisconfigured):
        build_registry(cfg)


def test_dotted_path_with_missing_class_raises_adapter_misconfigured() -> None:
    cfg = _config_with_type("tests._fixtures.raccoon_example_adapter:NotARealClass")
    with pytest.raises(AdapterMisconfigured):
        build_registry(cfg)


@pytest.mark.asyncio
async def test_dotted_path_adapter_serves_gateway_calls() -> None:
    cfg = _config_with_type(_DOTTED_PATH)
    registry = build_registry(cfg)
    service = DirectoryService(
        registry=registry,
        pattern_cache=PatternCache(max_entries=100, ttl_s=300.0),
        name_cache=PVNameCache(max_entries=100, positive_ttl_s=1800.0, negative_ttl_s=60.0),
        flight=SingleFlight(),
        config=cfg,
    )
    gateway.bind(service)
    try:
        assert await gateway.list_pvs("anything") == ["PV:EXAMPLE"]
        assert await gateway.list_devices("anything") == ["DEV:EXAMPLE"]
        assert await gateway.list_elements("anything") == ["EL:EXAMPLE"]
        assert await gateway.exists("PV:EXAMPLE") is True
        assert await gateway.exists("PV:NOPE") is False
    finally:
        gateway.bind(None)
        await registry.close()


@pytest.mark.asyncio
async def test_dotted_path_adapter_passes_contract_suite() -> None:
    """The example adapter conforms to the behavioral contract — the same
    sequence an out-of-tree lab runs against their own adapter."""
    from tests.canopy.raccoon.adapters import contract

    registry = build_registry(_config_with_type(_DOTTED_PATH))
    adapter = registry.require()
    try:
        await contract.assert_identity_attributes(adapter)
        await contract.assert_health(adapter)
        await contract.assert_list_pvs_invariants(adapter, pattern="anything")
        await contract.assert_list_devices_invariants(adapter, pattern="anything")
        await contract.assert_list_elements_invariants(adapter, pattern="anything")
        await contract.assert_close_idempotent(adapter)
    finally:
        await registry.close()
