"""The read-only CANOPY API forward: two WebSocket relays, 405 for everything else.

A fake upstream ``websockets`` server stands in for the canopy backend. It
records the handshake it received (path and headers), greets every connection,
and echoes text frames as ``echo:<text>`` and binary frames reversed. The text
``close-me`` makes it close with code 4000.
"""

import threading
from pathlib import Path
from collections.abc import Iterator

import pytest
from starlette.types import Send, Scope, ASGIApp, Receive
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
from websockets.sync.server import ServerConnection, serve

from canopy_mcp.app import create_app
from canopy_mcp.config import Settings
from canopy_mcp.forward import RELAYED_PATHS, SERVICE_PREFIXES, upstream_ws_url

KEY = "server-side-key"
CLIENT_KEY = "client-supplied-key"
PROXY_PREFIX = "/u/x/panel/canopy"


class FakeUpstream:
    """A threaded ``websockets`` server that records every handshake."""

    def __init__(self) -> None:
        self.requests: list[tuple[str, dict[str, list[str]]]] = []
        self._server = serve(self._handle, "127.0.0.1", 0)
        self.port = self._server.socket.getsockname()[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def _handle(self, ws: ServerConnection) -> None:
        headers: dict[str, list[str]] = {}
        for name, value in ws.request.headers.raw_items():
            headers.setdefault(name.lower(), []).append(value)
        self.requests.append((ws.request.path, headers))
        ws.send("welcome")
        for message in ws:
            if message == "close-me":
                ws.close(4000, "bye")
                return
            if isinstance(message, str):
                ws.send(f"echo:{message}")
            else:
                ws.send(bytes(reversed(message)))

    def stop(self) -> None:
        self._server.shutdown()
        self._thread.join(timeout=5)


@pytest.fixture
def upstream() -> Iterator[FakeUpstream]:
    server = FakeUpstream()
    yield server
    server.stop()


def _settings(tmp_path: Path, base_url: str, api_key: str = KEY) -> Settings:
    drafts = tmp_path / "drafts"
    drafts.mkdir(exist_ok=True)
    return Settings(base_url=base_url, api_key=api_key, drafts_dir=drafts)


@pytest.fixture
def client(tmp_path: Path, upstream: FakeUpstream) -> Iterator[TestClient]:
    app = create_app(_settings(tmp_path, f"http://127.0.0.1:{upstream.port}"), startup_hooks=[])
    with TestClient(app) as c:
        yield c


# --- upstream URL ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("base", "expected"),
    [
        ("http://api:8000", "ws://api:8000/hare/v1/ws/pvs"),
        ("https://api.example/", "wss://api.example/hare/v1/ws/pvs"),
        ("http://api:8000/canopy/", "ws://api:8000/canopy/hare/v1/ws/pvs"),
    ],
)
def test_upstream_url_maps_scheme_and_keeps_base_path(base: str, expected: str) -> None:
    assert upstream_ws_url(base, "/hare/v1/ws/pvs", []) == expected


def test_upstream_url_drops_api_key_and_keeps_other_query() -> None:
    url = upstream_ws_url(
        "http://api:8000", "/fox/v1/ws/formulas", [("api_key", "x"), ("a", "1"), ("API_KEY", "y"), ("b", "2")]
    )
    assert url == "ws://api:8000/fox/v1/ws/formulas?a=1&b=2"


def test_relayed_paths_are_exactly_the_two_streams() -> None:
    assert set(RELAYED_PATHS) == {"/hare/v1/ws/pvs", "/fox/v1/ws/formulas"}


# --- relays ----------------------------------------------------------------------


@pytest.mark.parametrize("path", ["/hare/v1/ws/pvs", "/fox/v1/ws/formulas"])
def test_relay_injects_key_and_relays_both_directions(client: TestClient, upstream: FakeUpstream, path: str) -> None:
    with client.websocket_connect(path) as ws:
        assert ws.receive_text() == "welcome"
        ws.send_text('{"type": "subscribe", "pvNames": ["X"]}')
        assert ws.receive_text() == 'echo:{"type": "subscribe", "pvNames": ["X"]}'
        ws.send_bytes(b"\x01\x02\x03")
        assert ws.receive_bytes() == b"\x03\x02\x01"

    [(seen_path, headers)] = upstream.requests
    assert seen_path == path
    assert headers["x-api-key"] == [KEY]


def test_relay_strips_client_key_from_query_and_header(client: TestClient, upstream: FakeUpstream) -> None:
    with client.websocket_connect(
        f"/hare/v1/ws/pvs?api_key={CLIENT_KEY}&keep=1", headers={"X-API-Key": CLIENT_KEY}
    ) as ws:
        assert ws.receive_text() == "welcome"

    [(seen_path, headers)] = upstream.requests
    assert seen_path == "/hare/v1/ws/pvs?keep=1"
    assert headers["x-api-key"] == [KEY]
    assert CLIENT_KEY not in seen_path
    assert all(CLIENT_KEY not in value for values in headers.values() for value in values)


def test_relay_sends_no_key_when_none_configured(tmp_path: Path, upstream: FakeUpstream) -> None:
    app = create_app(_settings(tmp_path, f"http://127.0.0.1:{upstream.port}", api_key=""), startup_hooks=[])
    with (
        TestClient(app) as c,
        c.websocket_connect(f"/hare/v1/ws/pvs?api_key={CLIENT_KEY}", headers={"X-API-Key": CLIENT_KEY}) as ws,
    ):
        assert ws.receive_text() == "welcome"

    [(seen_path, headers)] = upstream.requests
    assert seen_path == "/hare/v1/ws/pvs"
    assert "x-api-key" not in headers


def test_upstream_close_reaches_the_client(client: TestClient) -> None:
    with client.websocket_connect("/hare/v1/ws/pvs") as ws:
        assert ws.receive_text() == "welcome"
        ws.send_text("close-me")
        with pytest.raises(WebSocketDisconnect) as closed:
            ws.receive_text()
    assert closed.value.code == 4000


def test_unreachable_upstream_rejects_the_handshake(tmp_path: Path, upstream: FakeUpstream) -> None:
    port = upstream.port
    upstream.stop()
    app = create_app(_settings(tmp_path, f"http://127.0.0.1:{port}"), startup_hooks=[])
    with TestClient(app) as c, pytest.raises(WebSocketDisconnect):
        with c.websocket_connect("/hare/v1/ws/pvs") as ws:
            ws.receive_text()


class PrefixStrippingProxy:
    """Stands in for the OSPREY panel proxy: strips the panel prefix, adds the header."""

    def __init__(self, app: ASGIApp, prefix: str) -> None:
        self.app = app
        self.prefix = prefix

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] in ("http", "websocket"):
            path = scope["path"]
            assert path.startswith(self.prefix), path
            scope = dict(scope)
            scope["path"] = path[len(self.prefix) :] or "/"
            scope["raw_path"] = scope["path"].encode()
            scope["headers"] = [*scope["headers"], (b"x-forwarded-prefix", self.prefix.encode())]
        await self.app(scope, receive, send)


def test_viewer_url_shape_through_prefix_stripping_proxy(tmp_path: Path, upstream: FakeUpstream) -> None:
    app = create_app(_settings(tmp_path, f"http://127.0.0.1:{upstream.port}"), startup_hooks=[])
    with (
        TestClient(PrefixStrippingProxy(app, PROXY_PREFIX)) as c,
        c.websocket_connect(f"ws://testserver{PROXY_PREFIX}/hare/v1/ws/pvs") as ws,
    ):
        assert ws.receive_text() == "welcome"
        ws.send_text("ping")
        assert ws.receive_text() == "echo:ping"

    [(seen_path, headers)] = upstream.requests
    assert seen_path == "/hare/v1/ws/pvs"
    assert headers["x-api-key"] == [KEY]
    assert "x-forwarded-prefix" not in headers


# --- everything else is 405 ------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("PUT", "/hare/v1/pvs/X"),
        ("PUT", "/hare/v1/pvs/SR:C01:MAG:Q1"),
        ("POST", "/squirrel/v1/snapshots/abc/restore"),
        ("POST", "/squirrel/abc/restore"),
        ("GET", "/hare/v1/pvs"),
        ("GET", "/hare"),
        ("GET", "/hare/v1/ws/pvs"),
        ("GET", "/fox/v1/formulas"),
        ("POST", "/fox/v1/formulas"),
        ("GET", "/squirrel/v1/snapshots"),
        ("POST", "/raccoon/v1/screens"),
        ("DELETE", "/raccoon/v1/items/1"),
        ("PATCH", "/raccoon"),
    ],
)
def test_other_api_methods_and_paths_are_405(
    client: TestClient, upstream: FakeUpstream, method: str, path: str
) -> None:
    r = client.request(method, path, headers={"X-API-Key": CLIENT_KEY})
    assert r.status_code == 405
    assert upstream.requests == []


def test_beaver_is_not_a_service_prefix(client: TestClient, upstream: FakeUpstream) -> None:
    """Beaver is retired upstream: nothing under /beaver is a canopy service path any more."""
    assert SERVICE_PREFIXES == ("hare", "fox", "squirrel", "raccoon", "mole")
    r = client.get("/beaver/v1/widgets", headers={"X-API-Key": CLIENT_KEY})
    assert r.status_code != 405
    assert upstream.requests == []


@pytest.mark.parametrize("path", ["/hare/v1/ws/other", "/squirrel/v1/ws/pvs", "/hare/v1/pvs/X"])
def test_other_websocket_paths_are_refused(client: TestClient, upstream: FakeUpstream, path: str) -> None:
    with pytest.raises(WebSocketDisconnect), client.websocket_connect(path) as ws:
        ws.receive_text()
    assert upstream.requests == []


def test_existing_routes_still_answer(client: TestClient) -> None:
    assert client.get("/health").status_code == 200
