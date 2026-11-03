"""E-log-scoped pytest fixtures.

Builds an `ElogService` around `FakeElogAdapter`s and serves it through the
composed Canopy app, the same `gateway.get_service` path production uses.
"""

import pytest
from fastapi.testclient import TestClient

from canopy.core import security
from canopy.elog import gateway
from canopy.main import app as parent_app
from canopy.elog.app import app as elog_app
from canopy.elog.config import ElogConfig, ElogAppConfig
from canopy.core.security import EnvKeyRegistry
from canopy.elog.dependencies import get_elog_service
from tests.canopy.elog._fakes import FakeElogAdapter
from canopy.elog.adapters.registry import ElogRegistry
from canopy.elog.services.elog_service import ElogService

DEFAULT_LOGBOOK_IDS = ["lb-ops"]


def build_service(
    adapters: dict[str, FakeElogAdapter], *, enabled: bool = True, max_attachment_bytes: int = 20 * 1024 * 1024
) -> ElogService:
    config = ElogConfig(
        provider="fake" if enabled else "",
        apps={app: ElogAppConfig(token="t", default_logbook_ids=DEFAULT_LOGBOOK_IDS) for app in adapters},
        max_attachment_bytes=max_attachment_bytes,
    )
    if enabled:
        registry = ElogRegistry("fake", adapters)
    else:
        registry = ElogRegistry(
            None, disabled_reason="No e-log provider is configured (CANOPY_ELOG__PROVIDER is empty)"
        )
    return ElogService(config, registry)


@pytest.fixture
def squirrel_adapter() -> FakeElogAdapter:
    return FakeElogAdapter("squirrel")


@pytest.fixture
def elog_service(squirrel_adapter: FakeElogAdapter) -> ElogService:
    return build_service({"squirrel": squirrel_adapter})


@pytest.fixture
def elog_client(elog_service: ElogService):
    """TestClient on the composed app with the fake-backed service.

    Mounted sub-apps keep their own `dependency_overrides`, so the override
    goes on `elog_app`. The gateway bind happens after entering the client,
    because the parent lifespan binds its own env-configured service on startup.
    """
    elog_app.dependency_overrides[get_elog_service] = lambda: elog_service
    try:
        with TestClient(parent_app) as client:
            gateway.bind(elog_service)
            yield client
    finally:
        elog_app.dependency_overrides.pop(get_elog_service, None)
        gateway.bind(None)


@pytest.fixture
def bind_env_keys():
    """Install an env-only key registry (no database) for scope tests; restore afterward.

    Call it after the client has started, since the parent lifespan binds its own registry.
    """
    previous = security._registry

    def _bind(raw: str) -> None:
        security.bind_key_registry(EnvKeyRegistry(raw))

    yield _bind
    security._registry = previous
