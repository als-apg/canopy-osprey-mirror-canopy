"""Gateway facade behavior — bind/unbind, fetch_window delegation."""

import pytest

from canopy.mole import gateway
from canopy.mole.config import MoleConfig
from canopy.mole.adapters.registry import AdapterRegistry
from canopy.mole.services.archiver_service import ArchiverService

from tests.canopy.mole.adapters._fakes import FakeArchiverAdapter


def test_get_service_unbound_raises():
    gateway.bind(None)
    with pytest.raises(RuntimeError, match="before app startup"):
        gateway.get_service()


async def test_bind_and_fetch_window_delegates():
    fake = FakeArchiverAdapter(name="primary")
    registry = AdapterRegistry({"primary": fake}, default="primary")
    service = ArchiverService(registry, MoleConfig(default_archiver="primary"))

    gateway.bind(service)
    try:
        samples = await gateway.fetch_window("PV:TEST", 0, 10_000_000_000)
        assert len(samples) > 0
        assert all(s.source == "archive" for s in samples)
    finally:
        gateway.bind(None)


async def test_fetch_window_propagates_archiver_override():
    fake_a = FakeArchiverAdapter(name="a")
    fake_b = FakeArchiverAdapter(name="b")
    registry = AdapterRegistry({"a": fake_a, "b": fake_b}, default="a")
    service = ArchiverService(registry, MoleConfig(default_archiver="a"))

    gateway.bind(service)
    try:
        # Both should succeed; the override path uses the named adapter
        samples_a = await gateway.fetch_window("PV:TEST", 0, 1_000_000_000, archiver="a")
        samples_b = await gateway.fetch_window("PV:TEST", 0, 1_000_000_000, archiver="b")
        assert samples_a and samples_b
    finally:
        gateway.bind(None)
