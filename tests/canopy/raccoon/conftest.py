"""Raccoon-scoped pytest fixtures.

Builds a `DirectoryService` around a `FakeDirectoryAdapter` and binds it on the
gateway for the duration of the test. Routes (slice 5) consume it through the
same `gateway.get_service` path as production.
"""

import pytest
from fastapi.testclient import TestClient

from canopy.main import app as parent_app
from canopy.raccoon import gateway
from canopy.raccoon.app import app as raccoon_app
from canopy.raccoon.config import RaccoonConfig, CacheConfig, DirectoryInstanceConfig
from canopy.raccoon.cache.flight import SingleFlight
from canopy.raccoon.cache.name_cache import PVNameCache
from canopy.raccoon.cache.pattern_cache import PatternCache
from canopy.raccoon.adapters.registry import DirectoryRegistry
from canopy.raccoon.services.directory_service import DirectoryService
from tests.canopy.raccoon.adapters._fakes import FakeDirectoryAdapter


def _build_service(
    adapter: FakeDirectoryAdapter,
    *,
    pattern_ttl: float = 300.0,
    exists_ttl: float = 1800.0,
    negative_ttl: float = 60.0,
    timeout_s: float = 5.0,
) -> DirectoryService:
    cfg = RaccoonConfig(
        directory=DirectoryInstanceConfig(
            type="fake",
            timeout_s=timeout_s,
            cache=CacheConfig(
                pattern_ttl_s=pattern_ttl,
                exists_ttl_s=exists_ttl,
                negative_ttl_s=negative_ttl,
            ),
        )
    )
    registry = DirectoryRegistry(adapter)
    return DirectoryService(
        registry=registry,
        pattern_cache=PatternCache(max_entries=1000, ttl_s=pattern_ttl),
        name_cache=PVNameCache(max_entries=10000, positive_ttl_s=exists_ttl, negative_ttl_s=negative_ttl),
        flight=SingleFlight(),
        config=cfg,
    )


@pytest.fixture
def fake_adapter() -> FakeDirectoryAdapter:
    return FakeDirectoryAdapter()


@pytest.fixture
def directory_service(fake_adapter: FakeDirectoryAdapter) -> DirectoryService:
    return _build_service(fake_adapter)


@pytest.fixture
def make_directory_service():
    return _build_service


@pytest.fixture
def raccoon_client(directory_service: DirectoryService):
    """TestClient with `gateway.get_service` overridden to the fake-backed service.

    Mounted sub-apps maintain their own `dependency_overrides`, so we override
    on `raccoon_app` (not the parent). We also `bind()` the same service so
    `gateway.list_pvs(...)`-style direct calls work in tests.
    """
    from canopy.raccoon.dependencies import get_directory_service

    raccoon_app.dependency_overrides[get_directory_service] = lambda: directory_service
    gateway.bind(directory_service)
    try:
        with TestClient(parent_app) as client:
            yield client
    finally:
        raccoon_app.dependency_overrides.pop(get_directory_service, None)
        gateway.bind(None)
