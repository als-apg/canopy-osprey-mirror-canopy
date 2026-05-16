"""End-to-end: registry loads an out-of-tree adapter via dotted-path config,
and the loaded adapter passes the parameterized contract suite."""

import pytest

from canopy.mole.config import MoleConfig, ArchiverInstanceConfig
from canopy.mole.adapters.registry import build_registry

from tests._fixtures.example_adapter import ExampleAdapter
from tests.canopy.mole.adapters import contract


_DOTTED_PATH = "tests._fixtures.example_adapter:ExampleAdapter"


def test_registry_loads_dotted_path_adapter():
    config = MoleConfig(
        default_archiver="lab",
        archivers={"lab": ArchiverInstanceConfig(type=_DOTTED_PATH, url="http://nope")},
    )
    registry = build_registry(config)
    adapter = registry.get("lab")
    assert isinstance(adapter, ExampleAdapter)
    assert adapter.name == "lab"


@pytest.fixture
async def loaded_adapter():
    config = MoleConfig(
        default_archiver="lab",
        archivers={"lab": ArchiverInstanceConfig(type=_DOTTED_PATH, url="http://nope")},
    )
    registry = build_registry(config)
    adapter = registry.get("lab")
    yield adapter
    await registry.close_all()


async def test_dotted_path_adapter_passes_health(loaded_adapter):
    await contract.assert_health(loaded_adapter)


async def test_dotted_path_adapter_passes_fetch_window(loaded_adapter):
    await contract.assert_fetch_window_invariants(loaded_adapter, pv="PV:EXAMPLE", t0_ns=0, t1_ns=10_000_000_000)


async def test_dotted_path_adapter_passes_fetch_binned_auto(loaded_adapter):
    await contract.assert_fetch_binned_auto(
        loaded_adapter, pv="PV:EXAMPLE", t0_ns=0, t1_ns=10_000_000_000, target_buckets=5
    )


async def test_dotted_path_adapter_close_idempotent(loaded_adapter):
    await contract.assert_close_idempotent(loaded_adapter)
