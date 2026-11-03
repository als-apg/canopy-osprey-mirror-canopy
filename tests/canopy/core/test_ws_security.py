"""WebSocket auth via `ws_get_api_key` — the handshake path, distinct from HTTP.

A raw `WebSocketException` close (not JSON) is the only way to report a failed
handshake, so these pin the two paths HTTP auth doesn't have: an unknown token
and a downed key store, each closing with a different code so a client can tell
"you're not allowed" from "try again later".
"""

import pytest
from fastapi import FastAPI, Security, WebSocket
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from canopy.core import security
from canopy.core.errors import KeyStoreUnavailable
from canopy.core.modules import tag_module
from canopy.core.security import (
    APIKey,
    KeyRegistry,
    ws_get_api_key,
    ws_require_read_access,
)


class FakeRegistry:
    """Stands in for the production chain: resolves nothing, or is down."""

    def __init__(self, *, unavailable: bool = False, key: APIKey | None = None) -> None:
        self.unavailable = unavailable
        self.key = key

    async def get(self, token: str) -> APIKey | None:
        if self.unavailable:
            raise KeyStoreUnavailable("API key store is unavailable")
        return self.key


@pytest.fixture
def ws_app() -> FastAPI:
    app = FastAPI()
    tag_module(app, "core")

    @app.websocket("/ws")
    async def ws_route(websocket: WebSocket, key: APIKey = Security(ws_require_read_access)) -> None:
        await websocket.accept()

    sub_app = FastAPI()
    tag_module(sub_app, "formula-engine")

    @sub_app.websocket("/ws")
    async def sub_ws_route(websocket: WebSocket, key: APIKey = Security(ws_get_api_key)) -> None:
        await websocket.accept()

    @sub_app.websocket("/ws-read")
    async def sub_ws_read_route(websocket: WebSocket, key: APIKey = Security(ws_require_read_access)) -> None:
        await websocket.accept()

    app.mount("/formula-engine", sub_app)

    return app


@pytest.fixture
def bind_fake_registry():
    previous = security._registry

    def _bind(registry: KeyRegistry) -> None:
        security.bind_key_registry(registry)

    yield _bind
    security._registry = previous


class TestWsAuth:
    def test_unknown_token_closes_with_policy_violation(self, ws_app, bind_fake_registry):
        bind_fake_registry(FakeRegistry())
        client = TestClient(ws_app)

        with pytest.raises(WebSocketDisconnect) as exc_info:
            with client.websocket_connect("/ws?api_key=not-a-real-token"):
                pass

        assert exc_info.value.code == 1008
        assert "invalid_api_key" in exc_info.value.reason

    def test_store_outage_closes_with_internal_error(self, ws_app, bind_fake_registry):
        bind_fake_registry(FakeRegistry(unavailable=True))
        client = TestClient(ws_app)

        with pytest.raises(WebSocketDisconnect) as exc_info:
            with client.websocket_connect("/ws?api_key=whatever"):
                pass

        assert exc_info.value.code == 1011
        assert "key_store_unavailable" in exc_info.value.reason

    def test_out_of_scope_module_closes_with_policy_violation(self, ws_app, bind_fake_registry):
        squirrel_only = APIKey(token="t", read=True, write=False, modules=frozenset({"squirrel"}))
        bind_fake_registry(FakeRegistry(key=squirrel_only))
        client = TestClient(ws_app)

        with pytest.raises(WebSocketDisconnect) as exc_info:
            with client.websocket_connect("/formula-engine/ws?api_key=t"):
                pass

        assert exc_info.value.code == 1008
        assert "insufficient_module_access" in exc_info.value.reason

    def test_module_check_precedes_scope_check(self, ws_app, bind_fake_registry):
        # A key with neither read access nor module access must be reported as
        # out-of-scope, not out-of-read — module access is checked in
        # ws_get_api_key, upstream of ws_require_read_access.
        no_access = APIKey(token="t", read=False, write=False, modules=frozenset({"squirrel"}))
        bind_fake_registry(FakeRegistry(key=no_access))
        client = TestClient(ws_app)

        with pytest.raises(WebSocketDisconnect) as exc_info:
            with client.websocket_connect("/formula-engine/ws-read?api_key=t"):
                pass

        assert exc_info.value.code == 1008
        assert "insufficient_module_access" in exc_info.value.reason
