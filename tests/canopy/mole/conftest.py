"""Mole-scoped pytest fixtures."""

import pytest
from fastapi.testclient import TestClient

from canopy.main import app as parent_app
from canopy.mole.app import app as mole_app
from canopy.mole.config import MoleConfig
from canopy.mole.dependencies import get_archiver_service
from canopy.mole.adapters.registry import AdapterRegistry
from canopy.mole.services.archiver_service import ArchiverService

from tests.canopy.mole.adapters._fakes import FakeArchiverAdapter


@pytest.fixture
def test_service():
    """ArchiverService backed by a FakeArchiverAdapter named 'primary'."""
    primary = FakeArchiverAdapter(name="primary")
    registry = AdapterRegistry({"primary": primary}, default="primary")
    config = MoleConfig(default_archiver="primary")
    return ArchiverService(registry, config)


@pytest.fixture
def mole_client(test_service):
    """TestClient with `get_archiver_service` overridden to use the fake registry.

    Mounted sub-apps maintain their own `dependency_overrides`, so the override
    must go on `mole_app` (not the parent `canopy.main:app`).
    """
    mole_app.dependency_overrides[get_archiver_service] = lambda: test_service
    try:
        with TestClient(parent_app) as client:
            yield client
    finally:
        mole_app.dependency_overrides.pop(get_archiver_service, None)
