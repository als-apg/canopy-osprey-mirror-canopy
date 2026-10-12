"""The access credential in front of canopy_mcp, and the files its secrets come from.

With ``CANOPY_ACCESS_TOKEN_FILE`` set, nothing but ``GET /health`` answers a
request that does not carry the file's value as ``X-Canopy-Access``: every
route the app has is walked, in every method, plus both WebSockets. The second
half covers the secret files themselves, the inherited-socket launch and the
configured self URL.
"""

import os
import re
import sys
import json
import time
import signal
import socket
import threading
import subprocess
from pathlib import Path
from contextlib import contextmanager
from collections.abc import Iterator

import httpx
import pytest
from starlette.testclient import TestClient, WebSocketDenialResponse
from websockets.sync.server import serve

from canopy_mcp import __main__ as entry
from canopy_mcp.app import McpProxyGuard, create_app
from canopy_mcp.access import (
    ACCESS_HEADER,
    AccessGate,
    CredentialFileError,
    read_secret_file,
)
from canopy_mcp.client import API_KEY_HEADER, make_client, auth_headers
from canopy_mcp.config import Settings, get_settings
from tests.canopy_mcp.conftest import summary

TOKEN = "launch-credential-7f3a"
ACCESS = {ACCESS_HEADER: TOKEN}
OPEN = ("GET", "/health")
REFUSAL = {"code": "invalid_access_credential", "detail": "A valid X-Canopy-Access credential is required"}
ALL_METHODS = ("DELETE", "GET", "HEAD", "OPTIONS", "PATCH", "POST", "PUT")
SOCKETS = {"/hare/v1/ws/pvs", "/fox/v1/ws/formulas"}
MCP_HEADERS = {"accept": "application/json, text/event-stream", "content-type": "application/json"}
INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "test", "version": "0"},
    },
}
ENV_VARS = (
    "CANOPY_ACCESS_TOKEN_FILE",
    "CANOPY_API_KEY_FILE",
    "CANOPY_API_KEY",
    "CANOPY_SELF_URL",
    "CANOPY_BROWSER_SANDBOX",
)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _fill(path: str) -> str:
    """Replace every ``{param}`` / ``{param:convertor}`` with a concrete segment."""
    return re.sub(r"\{[^}]+\}", "x", path)


def _walk(app) -> Iterator[tuple[str, str, str]]:
    """Yield ``(kind, method, path)`` for every route; a route open to any method yields all seven."""
    for route in app.routes:
        kind = type(route).__name__
        assert kind != "Mount", "a mounted app would hide its routes from this walk"
        if "WebSocket" in kind:
            yield ("websocket", "", _fill(route.path))
        else:
            for method in sorted(route.methods or ALL_METHODS):
                yield ("http", method, _fill(route.path))


def _secret_file(tmp_path: Path, name: str, content: str) -> Path:
    path = tmp_path / name
    path.write_text(content)
    return path


def _settings(tmp_path: Path, **overrides) -> Settings:
    drafts = tmp_path / "drafts"
    drafts.mkdir(exist_ok=True)
    return Settings(**{"drafts_dir": drafts, "api_key": "k", **overrides})


def _run(args: list[str], tmp_path: Path, **env: str) -> subprocess.CompletedProcess:
    """Run a fresh interpreter, where ``canopy_mcp.app`` has not been imported yet."""
    base = {k: v for k, v in os.environ.items() if k not in ENV_VARS}
    return subprocess.run(
        [sys.executable, *args],
        env={**base, "CANOPY_DRAFTS_DIR": str(tmp_path / "drafts"), **env},
        capture_output=True,
        text=True,
        timeout=120,
    )


@contextmanager
def _upstream() -> Iterator[tuple[str, list[str | None]]]:
    """A backend WebSocket server; yields its base URL and the ``X-API-Key`` of every handshake."""
    keys: list[str | None] = []

    def handle(ws):
        keys.append(ws.request.headers.get("x-api-key"))
        ws.send("welcome")

    server = serve(handle, "127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.socket.getsockname()[1]}", keys
    finally:
        server.shutdown()
        thread.join(timeout=5)


@pytest.fixture
def gated_app(tmp_path: Path):
    token_file = _secret_file(tmp_path, "front.access", TOKEN + "\n")
    return create_app(_settings(tmp_path, access_token_file=token_file), startup_hooks=[])


@pytest.fixture
def routes(gated_app) -> list[tuple[str, str, str]]:
    return list(_walk(gated_app))


@pytest.fixture
def client(gated_app) -> Iterator[TestClient]:
    with TestClient(gated_app) as c:
        yield c


# --- The setting -------------------------------------------------------------------


def test_no_credential_is_asked_for_by_default(tmp_path):
    settings = _settings(tmp_path)
    assert settings.access_token_file is None
    app = create_app(settings, startup_hooks=[])
    assert [m.cls for m in app.user_middleware][0] is McpProxyGuard
    assert AccessGate not in [m.cls for m in app.user_middleware]


def test_the_token_file_is_read_from_the_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("CANOPY_ACCESS_TOKEN_FILE", str(tmp_path / "front.access"))
    assert Settings().access_token_file == tmp_path / "front.access"


@pytest.mark.parametrize("name", ["CANOPY_ACCESS_TOKEN_FILE", "CANOPY_API_KEY_FILE", "CANOPY_SELF_URL"])
def test_an_empty_variable_counts_as_unset(monkeypatch, name):
    monkeypatch.setenv(name, "")
    settings = Settings()
    assert (settings.access_token_file, settings.api_key_file, settings.self_url) == (None, None, None)


def test_the_browser_sandbox_switch_is_off_by_default_and_empty_counts_as_unset(monkeypatch):
    assert Settings().browser_sandbox is False
    monkeypatch.setenv("CANOPY_BROWSER_SANDBOX", "")
    assert Settings().browser_sandbox is False
    monkeypatch.setenv("CANOPY_BROWSER_SANDBOX", "1")
    assert Settings().browser_sandbox is True


def test_the_gated_app_hands_its_credential_to_the_screenshot_browser(tmp_path):
    """The browser's own requests to the gated viewer carry the file's credential; nothing is launched for it."""
    from canopy_mcp.app import chromium_hook

    token_file = _secret_file(tmp_path, "front.access", TOKEN + "\n")
    app = create_app(_settings(tmp_path, access_token_file=token_file), startup_hooks=[chromium_hook])
    with TestClient(app) as client:
        browser = app.state.browser
        assert browser.access_token == TOKEN
        assert browser.base_url == app.state.self_url
        assert not browser.running
        assert client.get("/health").status_code == 200
        assert client.get("/embed/drafts/x").status_code == 401  # the gate itself is unchanged


def test_the_gate_takes_the_place_of_the_proxy_guard(gated_app):
    classes = [m.cls for m in gated_app.user_middleware]
    assert classes[0] is AccessGate
    assert McpProxyGuard not in classes


# --- Every route, without the credential -------------------------------------------


def test_the_walk_reaches_the_mcp_route_the_viewer_the_stores_and_both_sockets(routes):
    """Guards the enumeration itself: a walk that finds nothing would pass every case below."""
    paths = {path for _, _, path in routes}
    assert {"/mcp", "/", "/config.json", "/drafts/manifest.json", "/views", "/views/x", "/mole/v1/x"} <= paths
    assert {path for kind, _, path in routes if kind == "websocket"} == SOCKETS
    assert ("http", *OPEN) in routes
    assert {method for _, method, path in routes if path == "/mcp"} == set(ALL_METHODS)


def test_every_http_route_and_method_is_refused_without_the_credential(client, routes):
    answered = {}
    for kind, method, path in routes:
        if kind == "http" and (method, path) != OPEN:
            resp = client.request(method, path, follow_redirects=False)
            if resp.status_code != 401 or (method != "HEAD" and resp.json() != REFUSAL):
                answered[f"{method} {path}"] = resp.status_code
    assert answered == {}


def test_every_method_is_refused_on_every_path_without_the_credential(client, routes):
    """Also the methods a route does not declare: the answer must not be the route's own 405."""
    answered = {}
    for path in sorted({path for _, _, path in routes}):
        for method in ALL_METHODS:
            if (method, path) != OPEN:
                resp = client.request(method, path, follow_redirects=False)
                if resp.status_code != 401:
                    answered[f"{method} {path}"] = resp.status_code
    assert answered == {}


def test_every_websocket_route_is_refused_before_it_is_accepted(client, routes):
    sockets = [path for kind, _, path in routes if kind == "websocket"]
    assert sockets
    for path in sockets:
        with pytest.raises(WebSocketDenialResponse) as excinfo:
            with client.websocket_connect(path):
                pass
        assert excinfo.value.status_code == 401, path
        assert excinfo.value.json() == REFUSAL


def test_a_websocket_on_a_path_with_no_route_is_refused_the_same_way(client):
    with pytest.raises(WebSocketDenialResponse) as excinfo:
        with client.websocket_connect("/no/such/socket"):
            pass
    assert excinfo.value.status_code == 401


def test_get_health_answers_without_the_credential_and_is_degraded_when_data_is_away(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "degraded", "data": "unavailable"}


@pytest.mark.real_data_summary
def test_get_health_without_the_credential_carries_the_channel_counts_and_nothing_else_of_the_summary(gated_app):
    body = summary(active_monitors=3, connected=2, subscriptions=3, notice="not the machine")
    with TestClient(gated_app) as c:
        transport = httpx.MockTransport(lambda request: httpx.Response(200, json=body))
        c.app.state.http = make_client(c.app.state.settings, transport=transport)
        resp = c.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "connected": 2, "total": 3}


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("HEAD", "/health"),
        ("POST", "/health"),
        ("GET", "/health/"),
        ("GET", "/health/summary"),
        ("GET", "/healthz"),
        ("GET", "/x/health"),
    ],
)
def test_only_get_on_the_exact_health_path_is_open(client, method, path):
    assert client.request(method, path, follow_redirects=False).status_code == 401


def test_a_path_that_matches_no_route_is_refused_rather_than_reported_missing(client):
    assert client.get("/no/such/route").status_code == 401


@pytest.mark.parametrize("value", ["", "wrong", TOKEN[:-1], TOKEN + "x", TOKEN.upper(), f" {TOKEN}x"])
def test_a_wrong_credential_is_refused(client, value):
    assert client.get("/config.json", headers={ACCESS_HEADER: value}).status_code == 401


def test_the_credential_is_not_taken_from_the_query_or_another_header(client):
    assert client.get(f"/config.json?x-canopy-access={TOKEN}&access={TOKEN}").status_code == 401
    assert client.get("/config.json", headers={"Authorization": f"Bearer {TOKEN}"}).status_code == 401
    assert client.get("/config.json", headers={"X-API-Key": TOKEN}).status_code == 401


def test_the_proxy_headers_alone_open_nothing(client):
    headers = {"x-forwarded-prefix": "/panel/canopy", "x-osprey-terminal-secret": "s"}
    assert client.get("/config.json", headers=headers).status_code == 401
    assert client.post("/mcp", json=INITIALIZE, headers={**MCP_HEADERS, **headers}).status_code == 401


def test_the_comparison_is_constant_time(monkeypatch, gated_app):
    import canopy_mcp.access as access

    compared = []

    def spy(left, right):
        compared.append((left, right))
        return left == right

    monkeypatch.setattr(access.hmac, "compare_digest", spy)
    client = TestClient(gated_app)
    assert client.get("/config.json", headers={ACCESS_HEADER: "wrong"}).status_code == 401
    assert client.get("/config.json", headers=ACCESS).status_code == 200
    assert compared == [(b"wrong", TOKEN.encode()), (TOKEN.encode(), TOKEN.encode())]


# --- With the credential -----------------------------------------------------------


def test_the_credential_opens_the_routes(client):
    assert client.get("/config.json", headers=ACCESS).status_code == 200
    assert client.get("/views", headers=ACCESS).status_code == 200
    assert client.get("/drafts/manifest.json", headers=ACCESS).status_code == 200
    # The header name is matched whatever its case.
    assert client.get("/config.json", headers={"x-canopy-access": TOKEN}).status_code == 200


def test_routes_keep_their_own_answers_behind_the_gate(client):
    assert client.put("/hare/v1/pvs/SOME:PV", headers=ACCESS, json={"value": 1}).status_code == 405
    assert client.get("/no/such/route", headers=ACCESS).status_code == 404


def test_mcp_answers_with_the_credential(client):
    r = client.post("/mcp", json=INITIALIZE, headers={**MCP_HEADERS, **ACCESS}, follow_redirects=False)
    assert r.status_code == 200
    assert "serverInfo" in r.text


@pytest.mark.parametrize("header", ["x-forwarded-prefix", "x-osprey-terminal-secret"])
def test_mcp_is_reachable_through_the_proxy_once_the_credential_replaces_the_guard(client, header):
    headers = {**MCP_HEADERS, **ACCESS, header: "/panel/canopy"}
    r = client.post("/mcp", json=INITIALIZE, headers=headers, follow_redirects=False)
    assert r.status_code == 200
    assert "serverInfo" in r.text


@pytest.mark.parametrize("method", ["GET", "POST", "DELETE"])
def test_mcp_with_a_trailing_slash_is_never_the_transport(client, method):
    refused = client.request(method, "/mcp/", json=INITIALIZE, headers=MCP_HEADERS, follow_redirects=False)
    assert refused.status_code == 401
    r = client.request(method, "/mcp/", json=INITIALIZE, headers={**MCP_HEADERS, **ACCESS}, follow_redirects=False)
    assert r.status_code in (307, 308, 404)
    assert "serverInfo" not in r.text
    if r.status_code != 404:
        assert r.headers["location"].endswith("/mcp")


def test_a_websocket_is_admitted_with_the_credential(tmp_path):
    token_file = _secret_file(tmp_path, "front.access", TOKEN)
    with _upstream() as (base_url, _):
        settings = _settings(tmp_path, base_url=base_url, access_token_file=token_file)
        with TestClient(create_app(settings, startup_hooks=[])) as c:
            with c.websocket_connect("/hare/v1/ws/pvs", headers=ACCESS) as ws:
                assert ws.receive_text() == "welcome"


def test_two_credential_headers_are_refused_even_when_one_is_right(gated_app):
    client = TestClient(gated_app)
    headers = [(ACCESS_HEADER, TOKEN), (ACCESS_HEADER, "caller-supplied")]
    assert client.get("/config.json", headers=headers).status_code == 401


# --- Servers without the denial-response extension ---------------------------------


async def _handshake(app, **scope):
    sent = []

    async def receive():
        return {"type": "websocket.connect"}

    async def send(message):
        sent.append(message)

    base = {"type": "websocket", "path": "/hare/v1/ws/pvs", "headers": [], "query_string": b"", "subprotocols": []}
    await app({**base, **scope}, receive, send)
    return sent


async def test_a_handshake_without_the_credential_is_closed_when_the_server_cannot_send_a_response(gated_app):
    sent = await _handshake(gated_app)
    assert [message["type"] for message in sent] == ["websocket.close"]
    assert sent[0]["code"] == 1008


async def test_a_handshake_without_the_credential_gets_a_401_when_the_server_can_send_one(gated_app):
    sent = await _handshake(gated_app, extensions={"websocket.http.response": {}})
    assert sent[0]["type"] == "websocket.http.response.start"
    assert sent[0]["status"] == 401
    assert "websocket.accept" not in [message["type"] for message in sent]


async def test_the_lifespan_scope_passes_the_gate():
    seen = []

    async def inner(scope, receive, send):
        seen.append(scope["type"])

    await AccessGate(inner, token=TOKEN)({"type": "lifespan"}, None, None)
    assert seen == ["lifespan"]


def test_the_gate_cannot_be_built_without_a_credential():
    with pytest.raises(ValueError):
        AccessGate(lambda *a: None, token="")


# --- Secret files ------------------------------------------------------------------


@pytest.mark.parametrize("content", [TOKEN, TOKEN + "\n", f"  {TOKEN}\r\n"])
def test_a_secret_file_yields_its_content_without_surrounding_whitespace(tmp_path, content):
    assert read_secret_file("CANOPY_ACCESS_TOKEN_FILE", _secret_file(tmp_path, "s", content)) == TOKEN


def test_a_missing_file_is_refused_and_named(tmp_path):
    path = tmp_path / "absent"
    with pytest.raises(CredentialFileError) as excinfo:
        read_secret_file("CANOPY_ACCESS_TOKEN_FILE", path)
    assert "CANOPY_ACCESS_TOKEN_FILE" in str(excinfo.value)
    assert str(path) in str(excinfo.value)


def test_a_directory_is_refused(tmp_path):
    with pytest.raises(CredentialFileError, match="cannot be read"):
        read_secret_file("CANOPY_API_KEY_FILE", tmp_path)


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads any file")
def test_an_unreadable_file_is_refused(tmp_path):
    path = _secret_file(tmp_path, "s", TOKEN)
    path.chmod(0)
    try:
        with pytest.raises(CredentialFileError, match="cannot be read"):
            read_secret_file("CANOPY_API_KEY_FILE", path)
    finally:
        path.chmod(0o600)


def test_a_file_that_is_not_text_is_refused(tmp_path):
    path = tmp_path / "s"
    path.write_bytes(b"\xff\xfe\x00secret")
    with pytest.raises(CredentialFileError, match="cannot be read"):
        read_secret_file("CANOPY_API_KEY_FILE", path)


@pytest.mark.parametrize("content", ["", "\n", "  \n\t"])
def test_an_empty_file_is_refused(tmp_path, content):
    with pytest.raises(CredentialFileError, match="holds no secret"):
        read_secret_file("CANOPY_API_KEY_FILE", _secret_file(tmp_path, "s", content))


@pytest.mark.parametrize("content", ["two words", "line-one\nline-two", "schlüssel", "bell\x07"])
def test_a_file_holding_more_than_one_token_is_refused_without_quoting_it(tmp_path, content):
    with pytest.raises(CredentialFileError) as excinfo:
        read_secret_file("CANOPY_API_KEY_FILE", _secret_file(tmp_path, "s", content))
    assert "single printable ASCII token" in str(excinfo.value)
    for word in content.split():
        assert word not in str(excinfo.value)


def test_an_app_is_not_built_on_a_bad_token_file(tmp_path):
    with pytest.raises(CredentialFileError, match="CANOPY_ACCESS_TOKEN_FILE"):
        create_app(_settings(tmp_path, access_token_file=tmp_path / "absent"), startup_hooks=[])


# --- The upstream read key from a file ---------------------------------------------


def test_the_key_file_supplies_the_upstream_key(tmp_path):
    settings = _settings(tmp_path, api_key="", api_key_file=_secret_file(tmp_path, "front.read", "file-key\n"))
    assert settings.api_key == "file-key"
    assert auth_headers(settings) == {API_KEY_HEADER: "file-key"}
    assert make_client(settings).headers[API_KEY_HEADER] == "file-key"


def test_the_key_file_wins_over_the_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("CANOPY_API_KEY", "env-key")
    monkeypatch.setenv("CANOPY_API_KEY_FILE", str(_secret_file(tmp_path, "front.read", "file-key")))
    assert get_settings().api_key == "file-key"
    assert make_client().headers[API_KEY_HEADER] == "file-key"


def test_a_bad_key_file_is_not_replaced_by_the_environment_key(monkeypatch, tmp_path):
    monkeypatch.setenv("CANOPY_API_KEY", "env-key")
    monkeypatch.setenv("CANOPY_API_KEY_FILE", str(tmp_path / "absent"))
    with pytest.raises(CredentialFileError, match="CANOPY_API_KEY_FILE"):
        Settings()


def test_the_environment_key_is_used_when_no_file_is_named(monkeypatch):
    monkeypatch.setenv("CANOPY_API_KEY", "env-key")
    assert Settings().api_key == "env-key"


def test_the_relays_send_the_file_key_upstream(tmp_path):
    key_file = _secret_file(tmp_path, "front.read", "file-key\n")
    with _upstream() as (base_url, keys):
        settings = _settings(tmp_path, base_url=base_url, api_key="env-key", api_key_file=key_file)
        with TestClient(create_app(settings, startup_hooks=[])) as c:
            with c.websocket_connect("/hare/v1/ws/pvs") as ws:
                assert ws.receive_text() == "welcome"
    assert keys == ["file-key"]


# --- A bad file stops the launch ---------------------------------------------------


@pytest.mark.parametrize("variable", ["CANOPY_ACCESS_TOKEN_FILE", "CANOPY_API_KEY_FILE"])
@pytest.mark.parametrize("content", [None, "", "\n"], ids=["missing", "empty", "blank"])
def test_the_launch_exits_non_zero_before_binding(tmp_path, variable, content):
    path = tmp_path / "secret"
    if content is not None:
        path.write_text(content)
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]

    proc = _run(["-m", "canopy_mcp", "--http", "--port", str(port)], tmp_path, **{variable: str(path)})

    assert proc.returncode != 0
    assert variable in proc.stderr
    assert str(path) in proc.stderr
    assert "Traceback" not in proc.stderr
    assert "Uvicorn running" not in proc.stderr + proc.stdout


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads any file")
@pytest.mark.parametrize("variable", ["CANOPY_ACCESS_TOKEN_FILE", "CANOPY_API_KEY_FILE"])
def test_the_launch_exits_non_zero_on_an_unreadable_file(tmp_path, variable):
    path = _secret_file(tmp_path, "secret", TOKEN)
    path.chmod(0)
    try:
        proc = _run(["-m", "canopy_mcp", "--http", "--port", "0"], tmp_path, **{variable: str(path)})
    finally:
        path.chmod(0o600)
    assert proc.returncode != 0
    assert str(path) in proc.stderr
    assert TOKEN not in proc.stderr + proc.stdout


@pytest.mark.parametrize("variable", ["CANOPY_ACCESS_TOKEN_FILE", "CANOPY_API_KEY_FILE"])
def test_importing_the_app_module_fails_on_a_bad_file(tmp_path, variable):
    path = tmp_path / "absent"
    proc = _run(["-c", "import canopy_mcp.app"], tmp_path, **{variable: str(path)})
    assert proc.returncode != 0
    assert "CredentialFileError" in proc.stderr
    assert str(path) in proc.stderr


def test_the_module_level_app_is_gated_when_the_environment_says_so(tmp_path):
    code = (
        "import json\n"
        "from starlette.testclient import TestClient\n"
        "import canopy_mcp.app\n"
        "client = TestClient(canopy_mcp.app.app)\n"
        f"access = {{'X-Canopy-Access': {TOKEN!r}}}\n"
        "print(json.dumps([client.get('/health').status_code, client.get('/config.json').status_code,\n"
        "                  client.get('/config.json', headers=access).status_code]))\n"
    )
    token_file = _secret_file(tmp_path, "front.access", TOKEN + "\n")
    proc = _run(["-c", code], tmp_path, CANOPY_ACCESS_TOKEN_FILE=str(token_file))
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout.strip().splitlines()[-1]) == [200, 401, 200]


# --- The launcher: --fd and the self URL -------------------------------------------


def _fake_launch(monkeypatch):
    """Replace ``create_app`` and uvicorn with recorders; returns ``(built, ran, served)``."""
    import uvicorn

    import canopy_mcp.app as app_mod

    built, ran, served = [], [], []
    sentinel = object()

    def fake_create_app(*args, **kwargs):
        built.append((args, kwargs))
        return sentinel

    class FakeServer:
        started = True

        def __init__(self, config):
            self.config = config

        def run(self, sockets=None):
            served.append((self.config, sockets))

    monkeypatch.setattr(app_mod, "create_app", fake_create_app)
    monkeypatch.setattr("uvicorn.run", lambda *a, **k: ran.append((a, k)))
    monkeypatch.setattr(uvicorn, "Config", lambda *a, **k: (a, k))
    monkeypatch.setattr(uvicorn, "Server", FakeServer)
    return built, ran, served, sentinel


def test_the_cli_has_no_descriptor_by_default():
    assert entry.parse_args(["--http"]).fd is None
    assert entry.parse_args(["--http", "--fd", "7"]).fd == 7


def test_without_fd_or_self_url_the_launch_is_what_it_always_was(monkeypatch):
    built, ran, served, sentinel = _fake_launch(monkeypatch)

    assert entry.main(["--http", "--host", "0.0.0.0", "--port", "9124"]) == 0

    assert built == [((), {"self_url": "http://127.0.0.1:9124"})]
    assert ran == [((sentinel,), {"host": "0.0.0.0", "port": 9124, "ws_per_message_deflate": False})]
    assert served == []


def test_the_configured_self_url_replaces_the_port_derived_one(monkeypatch):
    monkeypatch.setenv("CANOPY_SELF_URL", "http://127.0.0.1:41000")
    built, ran, served, sentinel = _fake_launch(monkeypatch)

    assert entry.main(["--http", "--port", "9124"]) == 0

    assert built == [((), {"self_url": "http://127.0.0.1:41000"})]
    assert ran == [((sentinel,), {"host": "127.0.0.1", "port": 9124, "ws_per_message_deflate": False})]


def test_fd_serves_on_the_inherited_socket_and_binds_nothing(monkeypatch):
    built, ran, served, sentinel = _fake_launch(monkeypatch)
    with socket.socket() as held:
        held.bind(("127.0.0.1", 0))
        held.listen()
        port = held.getsockname()[1]
        fd = os.dup(held.fileno())

        assert entry.main(["--http", "--fd", str(fd)]) == 0

    assert ran == []
    [(config, sockets)] = served
    assert config == ((sentinel,), {"ws_per_message_deflate": False})
    [listener] = sockets
    try:
        assert listener.fileno() == fd
        assert (listener.family, listener.type) == (socket.AF_INET, socket.SOCK_STREAM)
        # With no configured self URL, it is the port the inherited socket holds.
        assert built == [((), {"self_url": f"http://127.0.0.1:{port}"})]
    finally:
        listener.close()


def test_fd_with_a_configured_self_url_uses_it(monkeypatch):
    monkeypatch.setenv("CANOPY_SELF_URL", "http://127.0.0.1:41000")
    built, ran, served, _ = _fake_launch(monkeypatch)
    with socket.socket() as held:
        held.bind(("127.0.0.1", 0))
        held.listen()
        fd = os.dup(held.fileno())
        assert entry.main(["--http", "--fd", str(fd)]) == 0
    served[0][1][0].close()
    assert built == [((), {"self_url": "http://127.0.0.1:41000"})]


def test_fd_that_is_not_a_socket_exits_non_zero(monkeypatch, capsys, tmp_path):
    built, ran, served, _ = _fake_launch(monkeypatch)
    fd = os.open(tmp_path, os.O_RDONLY)
    try:
        assert entry.main(["--http", "--fd", str(fd)]) != 0
    finally:
        os.close(fd)
    assert (built, ran, served) == ([], [], [])
    assert f"--fd {fd}" in capsys.readouterr().err


def test_a_server_that_never_started_exits_non_zero(monkeypatch):
    import uvicorn

    built, ran, served, _ = _fake_launch(monkeypatch)
    monkeypatch.setattr(uvicorn.Server, "started", False)
    with socket.socket() as held:
        held.bind(("127.0.0.1", 0))
        held.listen()
        fd = os.dup(held.fileno())
        assert entry.main(["--http", "--fd", str(fd)]) != 0
    served[0][1][0].close()


def test_self_url_setting_reaches_an_app_built_without_one(tmp_path):
    app = create_app(_settings(tmp_path, self_url="http://127.0.0.1:41000/"), startup_hooks=[])
    assert app.state.self_url == "http://127.0.0.1:41000"


def test_an_explicit_self_url_beats_the_setting(tmp_path):
    settings = _settings(tmp_path, self_url="http://127.0.0.1:41000")
    app = create_app(settings, startup_hooks=[], self_url="http://127.0.0.1:9999")
    assert app.state.self_url == "http://127.0.0.1:9999"


def test_the_server_answers_on_an_inherited_socket(tmp_path):
    """The real thing: a parent holds the listener, the child serves on it with the gate on."""
    token_file = _secret_file(tmp_path, "front.access", TOKEN + "\n")
    # The startup hooks (backend catalog, Chromium) are not what is under test.
    code = (
        "import sys\n"
        "import canopy_mcp.app as app_mod\n"
        "from canopy_mcp.__main__ import main\n"
        "app_mod.STARTUP_HOOKS[:] = []\n"
        "sys.exit(main(['--http', '--fd', sys.argv[1]]))\n"
    )
    env = {k: v for k, v in os.environ.items() if k not in ENV_VARS}
    env.update(
        CANOPY_DRAFTS_DIR=str(tmp_path / "drafts"),
        CANOPY_ACCESS_TOKEN_FILE=str(token_file),
        CANOPY_PV_MODE="mock",
    )
    with socket.socket() as held:
        held.bind(("127.0.0.1", 0))
        held.listen()
        port = held.getsockname()[1]
        proc = subprocess.Popen(
            [sys.executable, "-c", code, str(held.fileno())],
            env=env,
            pass_fds=(held.fileno(),),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        try:
            base = f"http://127.0.0.1:{port}"
            deadline = time.monotonic() + 60
            health = None
            while time.monotonic() < deadline and proc.poll() is None:
                try:
                    health = httpx.get(f"{base}/health", timeout=2)
                    break
                except httpx.HTTPError:
                    time.sleep(0.1)
            assert health is not None and health.status_code == 200, proc.poll()
            assert health.json() == {"status": "ok"}
            assert httpx.get(f"{base}/config.json", timeout=5).status_code == 401
            assert httpx.get(f"{base}/config.json", headers=ACCESS, timeout=5).status_code == 200
        finally:
            proc.terminate()
            try:
                output, _ = proc.communicate(timeout=20)
            except subprocess.TimeoutExpired:
                proc.kill()
                output, _ = proc.communicate()
    # uvicorn shuts down cleanly and then re-raises the signal that stopped it.
    assert "Application shutdown complete" in output, output
    assert proc.returncode in (0, -signal.SIGTERM), output
