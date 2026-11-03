"""A facility a key still grants cannot be deleted out from under it.

The regression being pinned is the silent one: a grant stores a *slug*, so deleting
the facility leaves the key authenticating normally while its scope resolves to
``set()`` — a deny-all credential that looks healthy, diagnosed as an outage rather
than as a mis-provisioning. These tests live under ``keys`` rather than
``facilities`` because they need both tables, and this conftest builds the whole
``core`` schema.
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from canopy.core import security
from canopy.main import _facility_granted_to_key
from canopy.core.db import get_session
from canopy.core.errors import register_canopy_error_handler
from canopy.core.modules import WILDCARD, tag_module
from canopy.core.security import APIKey
from canopy.core.facilities import gateway as facilities_gateway
from canopy.core.facilities import service as facilities_service
from canopy.core.keys.service import ApiKeyService
from canopy.core.facilities.models import Facility
from canopy.core.facilities.routes import router as facilities_router

ADMIN_HEADERS = {"X-API-Key": "test-admin"}


@pytest.fixture
def facility_checkers():
    """Register the production key-grant checker, exactly as ``main``'s lifespan does,
    and restore the process-global registry afterwards."""
    previous = list(facilities_service._in_use_checkers)
    facilities_service._in_use_checkers.clear()
    facilities_gateway.register_in_use_checker(_facility_granted_to_key, "API key grants")
    yield
    facilities_service._in_use_checkers[:] = previous


@pytest.fixture
def admin_client(session_maker) -> TestClient:
    app = FastAPI()
    register_canopy_error_handler(app)
    tag_module(app, "core")
    app.include_router(facilities_router)

    async def override_get_session():
        async with session_maker() as s:
            try:
                yield s
                await s.commit()
            except Exception:
                await s.rollback()
                raise

    app.dependency_overrides[get_session] = override_get_session

    class FakeRegistry:
        async def get(self, token: str) -> APIKey | None:
            if token == "test-admin":
                return APIKey(
                    token=token,
                    read=True,
                    write=True,
                    admin=True,
                    modules=frozenset({WILDCARD}),
                    facilities=frozenset({WILDCARD}),
                    name="admin",
                )
            return None

    previous = security._registry
    security.bind_key_registry(FakeRegistry())
    try:
        yield TestClient(app, raise_server_exceptions=False)
    finally:
        security._registry = previous


async def test_granted_facility_cannot_be_deleted_and_the_key_keeps_its_scope(
    session, session_maker, admin_client, seeded_facilities, facility_checkers
):
    key = await ApiKeyService(session).create_key("trace", read=True, modules=["*"], facilities=["lcls-sc"])
    await session.commit()

    refused = admin_client.delete("/v1/facilities/lcls-sc", headers=ADMIN_HEADERS)

    assert refused.status_code == 400, refused.text
    assert refused.json()["code"] == "facility_in_use"
    assert "API key grants" in refused.json()["detail"]

    # The consequence that matters: the grant still resolves to a real facility, so
    # the key can still reach its data.
    scope = await facilities_gateway.resolve_key_facility_scope(frozenset(key.facilities))
    assert len(scope) == 1


async def test_deactivating_the_key_releases_the_facility(
    session, session_maker, admin_client, seeded_facilities, facility_checkers
):
    """A revoked key must not pin a facility in place forever."""
    key = await ApiKeyService(session).create_key("trace", read=True, modules=["*"], facilities=["lcls-sc"])
    await session.commit()
    assert admin_client.delete("/v1/facilities/lcls-sc", headers=ADMIN_HEADERS).status_code == 400

    await ApiKeyService(session).deactivate_key(key.id)
    await session.commit()

    assert admin_client.delete("/v1/facilities/lcls-sc", headers=ADMIN_HEADERS).status_code == 200


async def test_facility_no_key_grants_deletes_cleanly(
    session, session_maker, admin_client, seeded_facilities, facility_checkers
):
    """The guard is per-slug, not "any key has any grant" — a facility nobody named
    is still deletable while a granted sibling exists."""
    session.add(Facility(slug="facet", name="FACET-II"))
    await session.commit()
    await ApiKeyService(session).create_key("trace", read=True, modules=["*"], facilities=["lcls-sc"])
    await session.commit()

    assert admin_client.delete("/v1/facilities/facet", headers=ADMIN_HEADERS).status_code == 200
