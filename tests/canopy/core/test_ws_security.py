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
from canopy.core.security import APIKey, KeyRegistry, ws_require_read_access


class FakeRegistry:
    """Stands in for the production chain: resolves nothing, or is down."""

    def __init__(self, *, unavailable: bool = False) -> None:
        self.unavailable = unavailable

    async def get(self, token: str) -> APIKey | None:
        if self.unavailable:
            raise KeyStoreUnavailable("API key store is unavailable")
        return None


@pytest.fixture
def ws_app() -> FastAPI:
    app = FastAPI()

    @app.websocket("/ws")
    async def ws_route(websocket: WebSocket, key: APIKey = Security(ws_require_read_access)) -> None:
        await websocket.accept()

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
