"""Vole-scoped pytest fixtures."""

import pytest
from fastapi.testclient import TestClient

from canopy.main import app as parent_app
from canopy.vole.app import app as vole_app
from canopy.core.clock import FakeClock
from canopy.vole.config import VoleConfig
from canopy.vole.buffer.store import PVStore
from canopy.vole.dependencies import get_buffer_service
from canopy.vole.services.buffer_service import BufferService
from canopy.vole.services.eviction_service import EvictionService
from canopy.vole.services.alignment_service import AlignmentService


@pytest.fixture
def vole_config():
    return VoleConfig(
        max_points=1000,
        global_bytes_max=1_000_000,
    )


@pytest.fixture
def fake_clock():
    return FakeClock(start_ns=1_000_000_000_000)


@pytest.fixture
def test_service(vole_config, fake_clock):
    store = PVStore(vole_config)
    alignment = AlignmentService()
    eviction = EvictionService(store, vole_config)
    return BufferService(store, alignment, eviction, fake_clock, vole_config)


@pytest.fixture
def vole_client(test_service):
    """TestClient with Vole's BufferService overridden."""
    vole_app.dependency_overrides[get_buffer_service] = lambda: test_service
    try:
        with TestClient(parent_app) as client:
            yield client
    finally:
        vole_app.dependency_overrides.pop(get_buffer_service, None)
