"""Module scoping through `get_api_key`, exercised across real mount points.

A throwaway app with two sub-app mounts plus a `core` route stands in for the
production topology — enough to prove the check fires per-mount without pulling
in the ten real sub-apps and their runtimes.
"""

import pytest
from fastapi import FastAPI, Security
from fastapi.testclient import TestClient

from canopy.core import security
from canopy.core.errors import register_canopy_error_handler
from canopy.core.modules import WILDCARD, tag_module
from canopy.core.security import APIKey, require_read_access


class FakeRegistry:
    def __init__(self, key: APIKey) -> None:
        self.key = key

    async def get(self, token: str) -> APIKey | None:
        return self.key if token == self.key.token else None


@pytest.fixture
def scoped_app() -> FastAPI:
    app = FastAPI()
    register_canopy_error_handler(app)
    tag_module(app, "core")

    @app.get("/core-route")
    async def core_route(key: APIKey = Security(require_read_access)) -> dict[str, str]:
        return {"name": key.name}

    formula_engine_app = FastAPI()
    register_canopy_error_handler(formula_engine_app)
    tag_module(formula_engine_app, "formula-engine")

    @formula_engine_app.get("/read")
    async def formula_engine_route(key: APIKey = Security(require_read_access)) -> dict[str, str]:
        return {"name": key.name}

    squirrel_app = FastAPI()
    register_canopy_error_handler(squirrel_app)
    tag_module(squirrel_app, "squirrel")

    @squirrel_app.get("/read")
    async def squirrel_route(key: APIKey = Security(require_read_access)) -> dict[str, str]:
        return {"name": key.name}

    app.mount("/formula-engine", formula_engine_app)
    app.mount("/squirrel", squirrel_app)
    return app


@pytest.fixture
def bind_fake_registry():
    previous = security._registry

    def _bind(key: APIKey) -> None:
        security.bind_key_registry(FakeRegistry(key))

    yield _bind
    security._registry = previous


class TestModuleScoping:
    def test_wildcard_key_reaches_every_mount_and_the_parent_app(self, scoped_app, bind_fake_registry):
        bind_fake_registry(APIKey(token="t", read=True, write=False, modules=frozenset({WILDCARD}), name="wild"))
        client = TestClient(scoped_app)

        assert client.get("/core-route", headers={"X-API-Key": "t"}).status_code == 200
        assert client.get("/formula-engine/read", headers={"X-API-Key": "t"}).status_code == 200
        assert client.get("/squirrel/read", headers={"X-API-Key": "t"}).status_code == 200

    def test_scoped_key_is_refused_outside_its_module(self, scoped_app, bind_fake_registry):
        bind_fake_registry(
            APIKey(token="t", read=True, write=False, name="squirrel-only", modules=frozenset({"squirrel"}))
        )
        client = TestClient(scoped_app)

        ok = client.get("/squirrel/read", headers={"X-API-Key": "t"})
        refused = client.get("/formula-engine/read", headers={"X-API-Key": "t"})

        assert ok.status_code == 200
        assert refused.status_code == 403
        assert refused.json()["code"] == "insufficient_module_access"

    def test_module_check_precedes_scope_check(self, scoped_app, bind_fake_registry):
        # No read access AND out of scope — must report module access, not read.
        bind_fake_registry(APIKey(token="t", read=False, write=False, name="none", modules=frozenset({"squirrel"})))
        client = TestClient(scoped_app)

        resp = client.get("/formula-engine/read", headers={"X-API-Key": "t"})

        assert resp.status_code == 403
        assert resp.json()["code"] == "insufficient_module_access"
