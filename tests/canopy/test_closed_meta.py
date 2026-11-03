"""The closed-meta gate on the five modules that need no database or job queue.

With ``CANOPY_CLOSED_META=1`` nothing but ``GET /health`` answers without a
registry key, and a key opens only the modules it is scoped to and only if it
reads. Everything here must hold on an install without the ``data``
extra, so the file imports nothing from the unselected modules; the all-eleven
cases live in ``test_closed_meta_default.py``.
"""

import os
import re
import sys
import json
import asyncio
import subprocess

import pytest
from fastapi.testclient import TestClient
from starlette.testclient import WebSocketDenialResponse

import canopy.main
from canopy.core import security
from canopy.main import build_app
from tests.conftest import READ_HEADERS
from canopy.core.config import Settings
from canopy.core.errors import KeyStoreUnavailable

FIVE = ("hare", "vole", "formula-engine", "mole", "converter")
OPEN = ("GET", "/health")
REFUSAL = {"code": "invalid_api_key", "detail": "A valid API key is required"}


def _fill(path: str) -> str:
    """Replace every ``{param}`` / ``{param:convertor}`` with a concrete segment."""
    return re.sub(r"\{[^}]+\}", "x", path)


def _walk(app, prefix: str = ""):
    """Yield ``(kind, method, path)`` for every route, recursing into mounted apps."""
    for route in app.routes:
        kind = type(route).__name__
        if kind == "_IncludedRouter":
            # Newer FastAPI keeps each include_router() as one entry with no path of its own.
            yield from _walk(route.original_router, prefix + route.include_context.prefix)
            continue
        path = prefix + route.path
        if kind == "Mount":
            yield from _walk(route.app, path)
        elif "WebSocket" in kind:
            yield ("websocket", "", _fill(path))
        else:
            for method in sorted(route.methods):
                yield ("http", method, _fill(path))


def _run_python(code: str, tmp_path, **env) -> subprocess.CompletedProcess:
    """Run ``code`` in a fresh interpreter, where ``canopy.main`` has not been imported yet."""
    return subprocess.run(
        [sys.executable, "-c", code],
        cwd=tmp_path,
        env={**os.environ, **env},
        capture_output=True,
        text=True,
        timeout=120,
    )


@pytest.fixture(scope="module")
def closed_app():
    return build_app(FIVE, closed_meta=True)


@pytest.fixture(scope="module")
def routes(closed_app):
    return list(_walk(closed_app))


# --- The setting -------------------------------------------------------------------


def test_the_setting_is_off_by_default(monkeypatch):
    monkeypatch.delenv("CANOPY_CLOSED_META", raising=False)
    assert Settings().closed_meta is False


def test_the_setting_is_read_from_the_environment(monkeypatch):
    monkeypatch.setenv("CANOPY_CLOSED_META", "1")
    assert Settings().closed_meta is True


def test_an_app_built_without_the_gate_still_answers_meta_routes_keyless():
    client = TestClient(build_app(FIVE, closed_meta=False))
    assert client.get("/").status_code == 200
    assert client.get("/hare/openapi.json").status_code == 200


# --- Every route, without a key ----------------------------------------------------


def test_the_walk_reaches_the_parent_the_mounts_and_both_sockets(routes):
    """Guards the enumeration itself: a walk that finds nothing would pass every case below."""
    paths = {path for _, _, path in routes}
    assert {"/", "/health", "/docs", "/openapi.json"} <= paths
    for name in FIVE:
        assert f"/{name}/openapi.json" in paths
        assert f"/{name}/docs" in paths
        assert f"/{name}/v1/health/watchdog" in paths
    assert {path for kind, _, path in routes if kind == "websocket"} == {
        "/hare/v1/ws/pvs",
        "/formula-engine/v1/ws/formulas",
    }
    assert ("http", *OPEN) in routes


def test_every_http_route_and_method_is_refused_without_a_key(closed_app, routes):
    client = TestClient(closed_app)
    answered = {}
    for kind, method, path in routes:
        if kind == "http" and (method, path) != OPEN:
            resp = client.request(method, path)
            if resp.status_code != 401 or (method != "HEAD" and resp.json() != REFUSAL):
                answered[f"{method} {path}"] = resp.status_code
    assert answered == {}


def test_options_is_refused_on_every_path_without_a_key(closed_app, routes):
    client = TestClient(closed_app)
    answered = {}
    for path in sorted({path for _, _, path in routes}):
        resp = client.options(path)
        if resp.status_code != 401:
            answered[path] = resp.status_code
    assert answered == {}


def test_every_websocket_route_is_refused_without_a_key(closed_app, routes):
    client = TestClient(closed_app)
    sockets = [path for kind, _, path in routes if kind == "websocket"]
    for path in sockets:
        with pytest.raises(WebSocketDenialResponse) as excinfo:
            with client.websocket_connect(path):
                pass
        assert excinfo.value.status_code == 401, path
        assert excinfo.value.json() == REFUSAL


def test_get_health_answers_without_a_key(closed_app):
    resp = TestClient(closed_app).get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "healthy"}


@pytest.mark.parametrize(
    ("method", "path"),
    [("HEAD", "/health"), ("POST", "/health"), ("GET", "/health/"), ("GET", "/hare/health"), ("GET", "/healthz")],
)
def test_only_get_on_the_exact_health_path_is_open(closed_app, method, path):
    resp = TestClient(closed_app).request(method, path, follow_redirects=False)
    assert resp.status_code == 401


def test_a_path_that_matches_no_route_is_refused_rather_than_reported_missing(closed_app):
    assert TestClient(closed_app).get("/no/such/route").status_code == 401


def test_an_unknown_key_is_refused(closed_app):
    client = TestClient(closed_app)
    assert client.get("/", headers={"X-API-Key": "not-a-key"}).status_code == 401
    assert client.get("/", headers={"X-API-Key": ""}).status_code == 401


def test_a_registry_key_that_grants_no_scope_is_refused(closed_app):
    previous = security.get_registry()
    security.bind_key_registry(security.EnvKeyRegistry("scopeless:,test-read:read@*"))
    try:
        client = TestClient(closed_app)
        assert client.get("/", headers={"X-API-Key": "scopeless"}).status_code == 401
        assert client.get("/", headers=READ_HEADERS).status_code == 200
    finally:
        security.bind_key_registry(previous)


@pytest.mark.parametrize("path", ["/docs", "/openapi.json", "/hare/docs", "/hare/openapi.json"])
def test_the_docs_and_openapi_pages_are_refused_without_a_key(closed_app, path):
    resp = TestClient(closed_app).get(path)
    assert resp.status_code == 401
    assert resp.json() == REFUSAL


# --- Module scope and the read-key rule --------------------------------------------


@pytest.fixture
def scoped_keys():
    """Bind a registry of module-scoped keys for one test, then restore the previous one."""
    previous = security.get_registry()
    security.bind_key_registry(security.EnvKeyRegistry("hare-key:read@hare,hare-writer:write@hare,core-key:read@core"))
    try:
        yield
    finally:
        security.bind_key_registry(previous)


def test_a_key_scoped_to_one_module_opens_that_module(closed_app, scoped_keys):
    client = TestClient(closed_app)
    assert client.get("/hare/docs", headers={"X-API-Key": "hare-key"}).status_code == 200
    assert client.get("/hare/openapi.json", headers={"X-API-Key": "hare-key"}).status_code == 200


@pytest.mark.parametrize("path", ["/formula-engine/docs", "/formula-engine/openapi.json", "/vole/v1/health/watchdog"])
def test_a_key_scoped_to_another_module_is_refused_with_its_own_code(closed_app, scoped_keys, path):
    resp = TestClient(closed_app).get(path, headers={"X-API-Key": "hare-key"})
    assert resp.status_code == 403
    assert resp.json() == {
        "code": "insufficient_module_access",
        "detail": f"API key is not scoped to '{path.split('/')[1]}'",
    }


@pytest.mark.parametrize("path", ["/", "/docs", "/openapi.json", "/no/such/route"])
def test_the_parent_routes_belong_to_core(closed_app, scoped_keys, path):
    client = TestClient(closed_app)
    hare = client.get(path, headers={"X-API-Key": "hare-key"})
    assert hare.status_code == 403
    assert hare.json()["code"] == "insufficient_module_access"
    assert client.get(path, headers={"X-API-Key": "core-key"}).status_code != 403


def test_a_key_in_the_module_without_read_is_refused(closed_app, scoped_keys):
    resp = TestClient(closed_app).get("/hare/docs", headers={"X-API-Key": "hare-writer"})
    assert resp.status_code == 403
    assert resp.json()["code"] == "insufficient_read_access"


@pytest.fixture
def admin_only_key():
    """Bind a provisioning key that administers but does not read, then restore the previous registry."""
    previous = security.get_registry()
    security.bind_key_registry(security.EnvKeyRegistry("ops:admin@*"))
    try:
        yield {"X-API-Key": "ops"}
    finally:
        security.bind_key_registry(previous)


@pytest.mark.parametrize("path", ["/v1/facilities", "/v1/api-keys"])
def test_an_admin_only_key_passes_the_gate_on_key_and_facility_management(closed_app, admin_only_key, path):
    """Those routes take an admin key without read themselves, so the gate admits it to them."""
    gate = closed_app.middleware_stack
    while type(gate).__name__ != "_ClosedMeta":
        gate = gate.app
    scope = {"type": "http", "method": "GET", "path": path, "headers": [(b"x-api-key", b"ops")], "query_string": b""}
    assert asyncio.run(gate._refusal(scope)) is None


@pytest.mark.parametrize("path", ["/metrics", "/", "/docs", "/openapi.json"])
def test_an_admin_only_key_is_refused_by_every_other_core_route(closed_app, admin_only_key, path):
    resp = TestClient(closed_app).get(path, headers=admin_only_key)
    assert resp.status_code == 403
    assert resp.json()["code"] == "insufficient_read_access"


def test_an_admin_only_key_is_refused_by_a_data_module(closed_app, admin_only_key):
    resp = TestClient(closed_app).get("/hare/docs", headers=admin_only_key)
    assert resp.status_code == 403
    assert resp.json()["code"] == "insufficient_read_access"


def test_a_handshake_to_another_module_is_refused_with_403(closed_app, scoped_keys):
    client = TestClient(closed_app)
    with pytest.raises(WebSocketDenialResponse) as excinfo:
        with client.websocket_connect("/formula-engine/v1/ws/formulas?api_key=hare-key"):
            pass
    assert excinfo.value.status_code == 403
    assert excinfo.value.json()["code"] == "insufficient_module_access"


class _DownRegistry:
    async def get(self, token: str):
        raise KeyStoreUnavailable("API key store unavailable")


@pytest.fixture
def store_down():
    previous = security.get_registry()
    security.bind_key_registry(_DownRegistry())
    try:
        yield
    finally:
        security.bind_key_registry(previous)


def test_a_key_store_that_cannot_answer_is_a_503(closed_app, store_down):
    client = TestClient(closed_app)
    resp = client.get("/hare/docs", headers=READ_HEADERS)
    assert resp.status_code == 503
    assert resp.json() == {"code": "key_store_unavailable", "detail": "API key store unavailable"}
    assert client.get("/health").status_code == 200


async def test_a_handshake_is_closed_1011_when_the_key_store_cannot_answer(closed_app, store_down):
    sent = await _handshake(closed_app, query_string=b"api_key=test-read")
    assert [message["type"] for message in sent] == ["websocket.close"]
    assert sent[0]["code"] == 1011
    assert sent[0]["reason"].startswith("key_store_unavailable")


def test_a_key_store_down_answers_a_handshake_with_503_when_the_server_can(closed_app, store_down):
    with pytest.raises(WebSocketDenialResponse) as excinfo:
        with TestClient(closed_app).websocket_connect("/hare/v1/ws/pvs", headers=READ_HEADERS):
            pass
    assert excinfo.value.status_code == 503


# --- The key-management routes -----------------------------------------------------


def test_without_the_database_tier_no_key_management_is_mounted(monkeypatch):
    """``/v1/api-keys`` and ``/v1/facilities`` need the database tier; that case is in ``core/keys/test_router_mount.py``."""
    monkeypatch.setattr(canopy.main, "has_database_tier", lambda: False)
    app = build_app(FIVE, closed_meta=False)
    assert not any(path.startswith(("/v1/api-keys", "/v1/facilities")) for _, _, path in _walk(app))


def test_the_query_parameter_does_not_open_an_http_route(closed_app):
    assert TestClient(closed_app).get("/?api_key=test-read").status_code == 401


# --- Outside CORS ------------------------------------------------------------------


def test_a_keyless_preflight_is_answered_by_cors(closed_app):
    resp = TestClient(closed_app).options(
        "/hare/v1/health/summary",
        headers={"Origin": "https://example.org", "Access-Control-Request-Method": "GET"},
    )
    assert resp.status_code == 200
    assert "access-control-allow-origin" in resp.headers


@pytest.mark.parametrize(
    "headers",
    [{}, {"Origin": "https://example.org"}, {"Access-Control-Request-Method": "GET"}],
    ids=["bare", "origin-only", "method-only"],
)
def test_an_options_request_that_is_not_a_preflight_is_refused(closed_app, headers):
    resp = TestClient(closed_app).options("/hare/v1/health/summary", headers=headers)
    assert resp.status_code == 401
    assert "access-control-allow-origin" not in resp.headers


def test_the_request_after_a_preflight_still_needs_a_key(closed_app):
    resp = TestClient(closed_app).get("/hare/v1/health/summary", headers={"Origin": "https://example.org"})
    assert resp.status_code == 401


def test_a_preflight_with_a_key_is_answered_by_cors(closed_app):
    resp = TestClient(closed_app).options(
        "/hare/v1/health/summary",
        headers={"Origin": "https://example.org", "Access-Control-Request-Method": "GET", **READ_HEADERS},
    )
    assert resp.status_code == 200
    assert "access-control-allow-origin" in resp.headers


# --- With a key --------------------------------------------------------------------


@pytest.mark.parametrize("path", ["/", "/docs", "/openapi.json", "/hare/openapi.json", "/hare/v1/health/watchdog"])
def test_a_registry_key_opens_the_routes_that_have_no_key_dependency(closed_app, path):
    with TestClient(closed_app) as client:
        assert client.get(path, headers=READ_HEADERS).status_code == 200


def test_routes_keep_their_own_scope_checks(closed_app):
    """A read key passes the gate, and a write route still turns it away itself."""
    client = TestClient(closed_app)
    resp = client.put("/hare/v1/pvs/SOME:PV", headers=READ_HEADERS, json={"value": 1})
    assert resp.status_code == 403
    assert resp.json()["code"] == "insufficient_write_access"


def test_a_websocket_is_admitted_with_the_key_in_the_query(closed_app):
    with TestClient(closed_app) as client:
        with client.websocket_connect("/hare/v1/ws/pvs?api_key=test-read") as ws:
            ws.close()


def test_a_websocket_is_admitted_with_the_key_in_the_header(closed_app):
    with TestClient(closed_app) as client:
        with client.websocket_connect("/hare/v1/ws/pvs", headers=READ_HEADERS) as ws:
            ws.close()


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


async def test_a_keyless_handshake_is_closed_when_the_server_cannot_send_a_response(closed_app):
    sent = await _handshake(closed_app)
    assert [message["type"] for message in sent] == ["websocket.close"]
    assert sent[0]["code"] == 1008


async def test_a_keyless_handshake_gets_a_401_when_the_server_can_send_one(closed_app):
    sent = await _handshake(closed_app, extensions={"websocket.http.response": {}})
    assert sent[0]["type"] == "websocket.http.response.start"
    assert sent[0]["status"] == 401


# --- From the environment ----------------------------------------------------------


def test_canopy_main_app_is_closed_when_the_environment_says_so(tmp_path):
    code = (
        "import json\n"
        "from fastapi.testclient import TestClient\n"
        "import canopy.main\n"
        "client = TestClient(canopy.main.app)\n"
        "key = {'X-API-Key': 'test-read'}\n"
        "print(json.dumps([client.get('/health').status_code, client.get('/').status_code,\n"
        "                  client.get('/docs').status_code, client.get('/', headers=key).status_code]))\n"
    )
    proc = _run_python(code, tmp_path, CANOPY_MODULES=",".join(FIVE), CANOPY_CLOSED_META="1")
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout.strip().splitlines()[-1]) == [200, 401, 401, 200]
