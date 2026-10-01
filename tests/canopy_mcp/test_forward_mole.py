"""The GET-only Mole forward: ``GET /mole/v1/{path}`` relayed to the canopy backend.

The app's shared ``app.state.http`` client is swapped, after the lifespan has
started, for one built by ``make_client`` over an ``httpx.MockTransport`` whose
handler records every upstream request. Nothing reaches a network.
"""

from pathlib import Path
from collections.abc import Callable, Iterator

import httpx
import pytest
from starlette.testclient import TestClient

from canopy_mcp.app import create_app
from canopy_mcp.client import make_client
from canopy_mcp.config import Settings

KEY = "server-side-key"
CLIENT_KEY = "client-supplied-key"
BASE = "http://canopy-api:8000"

Handler = Callable[[httpx.Request], httpx.Response]


class Upstream:
    """Records upstream requests and answers them with ``self.respond``."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.respond: Handler = lambda request: httpx.Response(
            200, json={"ok": True}, headers={"content-type": "application/json"}
        )

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.respond(request)


def _settings(tmp_path: Path, base_url: str = BASE, api_key: str = KEY) -> Settings:
    drafts = tmp_path / "drafts"
    drafts.mkdir(exist_ok=True)
    return Settings(base_url=base_url, api_key=api_key, drafts_dir=drafts)


def _client(tmp_path: Path, upstream: Upstream, base_url: str = BASE) -> Iterator[TestClient]:
    settings = _settings(tmp_path, base_url)
    app = create_app(settings, startup_hooks=[])
    with TestClient(app) as c:
        app.state.http = make_client(settings, transport=httpx.MockTransport(upstream))
        yield c


@pytest.fixture
def upstream() -> Upstream:
    return Upstream()


@pytest.fixture
def client(tmp_path: Path, upstream: Upstream) -> Iterator[TestClient]:
    yield from _client(tmp_path, upstream)


# --- headers ---------------------------------------------------------------------


def test_server_key_and_accept_are_forwarded_and_nothing_else_from_the_client(
    client: TestClient, upstream: Upstream
) -> None:
    r = client.get(
        "/mole/v1/archivers",
        headers={
            "Accept": "application/json",
            "X-API-Key": CLIENT_KEY,
            "Authorization": "Bearer client-token",
            "Cookie": "session=abc",
            "X-Forwarded-For": "10.0.0.1",
            "X-Forwarded-Prefix": "/u/x/panel/canopy",
            "X-Custom": "nope",
        },
    )
    assert r.status_code == 200
    [request] = upstream.requests
    assert request.headers.get_list("x-api-key") == [KEY]
    assert request.headers["accept"] == "application/json"
    for name in ("cookie", "authorization", "x-custom"):
        assert name not in request.headers
    assert not [name for name in request.headers if name.lower().startswith("x-forwarded")]


def test_api_key_query_parameter_is_dropped(client: TestClient, upstream: Upstream) -> None:
    r = client.get("/mole/v1/data/SR:PV?from=10&api_key=leak&API_KEY=leak2&to=20")
    assert r.status_code == 200
    [request] = upstream.requests
    params = request.url.params
    assert "api_key" not in {name.lower() for name in params}
    assert params.get_list("from") == ["10"]
    assert params.get_list("to") == ["20"]
    assert b"leak" not in request.url.raw_path


# --- path ------------------------------------------------------------------------


def test_archivers_path_arrives_unchanged(client: TestClient, upstream: Upstream) -> None:
    assert client.get("/mole/v1/archivers").status_code == 200
    [request] = upstream.requests
    assert request.url.raw_path == b"/mole/v1/archivers"
    assert request.url.host == "canopy-api"
    assert request.url.port == 8000


def test_encoded_slash_inside_a_segment_survives(client: TestClient, upstream: Upstream) -> None:
    assert client.get("/mole/v1/data/A%2FB").status_code == 200
    [request] = upstream.requests
    assert request.url.raw_path == b"/mole/v1/data/A%2FB"


def test_base_url_path_is_prefixed_once(tmp_path: Path, upstream: Upstream) -> None:
    for c in _client(tmp_path, upstream, base_url="http://canopy-api:8000/api/"):
        assert c.get("/mole/v1/archivers?x=1").status_code == 200
    [request] = upstream.requests
    assert request.url.raw_path == b"/api/mole/v1/archivers?x=1"


@pytest.mark.parametrize(
    "path",
    [
        "/mole/v1/%2e%2e/hare/v1/pvs",
        "/mole/v1/data/..%2F..%2Fhare",
        "/mole/v1/%2E%2E/%2E%2E/hare/v1/pvs",
        "/mole/v1/data/%252e%252e/%252e%252e/%252e%252e/hare",
    ],
)
def test_paths_escaping_mole_v1_are_404_without_upstream_request(
    client: TestClient, upstream: Upstream, path: str
) -> None:
    r = client.get(path)
    assert r.status_code == 404
    assert upstream.requests == []


@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE", "PATCH"])
def test_non_get_methods_are_405(client: TestClient, upstream: Upstream, method: str) -> None:
    r = client.request(method, "/mole/v1/archivers")
    assert r.status_code == 405
    assert upstream.requests == []


@pytest.mark.parametrize("path", ["/mole", "/mole/v2/archivers"])
def test_other_mole_paths_are_405(client: TestClient, upstream: Upstream, path: str) -> None:
    assert client.get(path).status_code == 405
    assert upstream.requests == []


# --- relay -----------------------------------------------------------------------


def test_status_body_and_content_type_are_relayed(client: TestClient, upstream: Upstream) -> None:
    body = b'{"code": "pv_not_found_in_archiver", "detail": "NO:SUCH:PV"}'
    upstream.respond = lambda request: httpx.Response(
        404, content=body, headers={"content-type": "application/problem+json", "x-upstream": "secret"}
    )
    r = client.get("/mole/v1/data/NO:SUCH:PV")
    assert r.status_code == 404
    assert r.content == body
    assert r.headers["content-type"] == "application/problem+json"
    assert "x-upstream" not in r.headers


def test_timeout_is_504_archiver_timeout_with_15s_read_timeout(client: TestClient, upstream: Upstream) -> None:
    def slow(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    upstream.respond = slow
    r = client.get("/mole/v1/data/SR:PV")
    assert r.status_code == 504
    assert r.json()["code"] == "archiver_timeout"
    assert isinstance(r.json()["detail"], str)
    [request] = upstream.requests
    assert request.extensions["timeout"]["read"] == 15.0


def test_transport_error_is_502_in_the_envelope(client: TestClient, upstream: Upstream) -> None:
    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    upstream.respond = down
    r = client.get("/mole/v1/archivers")
    assert r.status_code == 502
    body = r.json()
    assert set(body) == {"code", "detail"}
    assert body["code"] == "archiver_unavailable"
