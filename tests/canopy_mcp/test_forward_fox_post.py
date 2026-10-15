"""The Fox POST relay: ``POST /fox/v1/validate`` and ``POST /fox/v1/evaluate`` (A41).

Both are read access on Fox and are forwarded under the Mole GET forward's
rules: the server key goes upstream, no client header but ``Content-Type`` and
``Accept`` is copied, ``api_key`` is dropped, the path is contained, the body is
capped, the upstream call has a timeout, and a cross-site ``Sec-Fetch-Site`` is
refused. Every other Fox HTTP path still answers 405.

The app's shared ``app.state.http`` client is swapped, after the lifespan has
started, for one built by ``make_client`` over an ``httpx.MockTransport`` whose
handler records every upstream request. Nothing reaches a network.
"""

import json
from pathlib import Path
from collections.abc import Callable, Iterator

import httpx
import pytest
from starlette.testclient import TestClient

from canopy_mcp.app import create_app
from canopy_mcp.client import make_client
from canopy_mcp.config import Settings
from canopy_mcp.forward import FOX_TIMEOUT, FOX_POST_PATHS, FOX_BODY_MAX_BYTES

KEY = "server-side-key"
CLIENT_KEY = "client-supplied-key"
BASE = "http://canopy-api:8000"
SPEC = {"spec": {"kind": "scalar", "expression": "a + 1", "bindings": {"a": "SR:PV"}}}
BODY = json.dumps(SPEC).encode()

Handler = Callable[[httpx.Request], httpx.Response]


class Upstream:
    """Records upstream requests (and their bodies) and answers them with ``self.respond``."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.bodies: list[bytes] = []
        self.respond: Handler = lambda request: httpx.Response(
            200, json={"valid": True, "tokens": [], "errors": []}, headers={"content-type": "application/json"}
        )

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        self.bodies.append(request.read())
        return self.respond(request)


def _client(tmp_path: Path, upstream: Upstream, base_url: str = BASE) -> Iterator[TestClient]:
    drafts = tmp_path / "drafts"
    drafts.mkdir(exist_ok=True)
    settings = Settings(base_url=base_url, api_key=KEY, drafts_dir=drafts)
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


def test_exactly_validate_and_evaluate_are_relayed() -> None:
    assert set(FOX_POST_PATHS) == {"/fox/v1/validate", "/fox/v1/evaluate"}


# --- relay -----------------------------------------------------------------------


@pytest.mark.parametrize("path", ["/fox/v1/validate", "/fox/v1/evaluate"])
def test_body_status_and_content_type_are_relayed(client: TestClient, upstream: Upstream, path: str) -> None:
    answer = b'{"code": "formula_parse_error", "detail": "unexpected token"}'
    upstream.respond = lambda request: httpx.Response(
        422, content=answer, headers={"content-type": "application/problem+json", "x-upstream": "secret"}
    )
    r = client.post(path, content=BODY, headers={"Content-Type": "application/json"})
    assert r.status_code == 422
    assert r.content == answer
    assert r.headers["content-type"] == "application/problem+json"
    assert "x-upstream" not in r.headers
    [request] = upstream.requests
    assert request.method == "POST"
    assert request.url.raw_path == path.encode()
    assert request.url.host == "canopy-api"
    assert request.url.port == 8000
    assert upstream.bodies == [BODY]


@pytest.mark.parametrize("path", ["/fox/v1/validate", "/fox/v1/evaluate"])
def test_success_is_relayed(client: TestClient, upstream: Upstream, path: str) -> None:
    r = client.post(path, json=SPEC)
    assert r.status_code == 200
    assert r.json() == {"valid": True, "tokens": [], "errors": []}
    assert json.loads(upstream.bodies[0]) == SPEC


def test_base_url_path_is_prefixed_once(tmp_path: Path, upstream: Upstream) -> None:
    for c in _client(tmp_path, upstream, base_url="http://canopy-api:8000/api/"):
        assert c.post("/fox/v1/evaluate?x=1", content=BODY).status_code == 200
    [request] = upstream.requests
    assert request.url.raw_path == b"/api/fox/v1/evaluate?x=1"


# --- headers and query -----------------------------------------------------------


@pytest.mark.parametrize("path", ["/fox/v1/validate", "/fox/v1/evaluate"])
def test_server_key_goes_upstream_and_no_other_client_header(client: TestClient, upstream: Upstream, path: str) -> None:
    r = client.post(
        path,
        content=BODY,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "X-API-Key": CLIENT_KEY,
            "Authorization": "Bearer client-token",
            "Cookie": "session=abc",
            "Origin": "http://evil.example",
            "Sec-Fetch-Site": "same-origin",
            "X-Forwarded-For": "10.0.0.1",
            "X-Forwarded-Prefix": "/u/x/panel/canopy",
            "X-Custom": "nope",
        },
    )
    assert r.status_code == 200
    [request] = upstream.requests
    assert request.headers.get_list("x-api-key") == [KEY]
    assert CLIENT_KEY not in str(request.headers)
    assert request.headers["content-type"] == "application/json"
    assert request.headers["accept"] == "application/json"
    for name in ("cookie", "authorization", "origin", "x-custom", "sec-fetch-site"):
        assert name not in request.headers
    assert not [name for name in request.headers if name.lower().startswith("x-forwarded")]
    # Everything else on the request is httpx's own transport framing.
    assert set(request.headers) <= {
        "host",
        "accept",
        "accept-encoding",
        "connection",
        "user-agent",
        "content-length",
        "content-type",
        "x-api-key",
    }


def test_api_key_query_parameter_is_dropped(client: TestClient, upstream: Upstream) -> None:
    r = client.post("/fox/v1/validate?a=1&api_key=leak&API_KEY=leak2&b=2", content=BODY)
    assert r.status_code == 200
    [request] = upstream.requests
    params = request.url.params
    assert "api_key" not in {name.lower() for name in params}
    assert params.get_list("a") == ["1"]
    assert params.get_list("b") == ["2"]
    assert b"leak" not in request.url.raw_path


def test_client_key_in_query_never_reaches_upstream(client: TestClient, upstream: Upstream) -> None:
    r = client.post(f"/fox/v1/evaluate?api_key={CLIENT_KEY}", content=BODY, headers={"X-API-Key": CLIENT_KEY})
    assert r.status_code == 200
    [request] = upstream.requests
    assert CLIENT_KEY.encode() not in request.url.raw_path
    assert request.headers.get_list("x-api-key") == [KEY]


# --- body cap --------------------------------------------------------------------


def test_body_at_the_cap_is_relayed(client: TestClient, upstream: Upstream) -> None:
    body = b"x" * FOX_BODY_MAX_BYTES
    assert client.post("/fox/v1/validate", content=body).status_code == 200
    assert upstream.bodies == [body]


@pytest.mark.parametrize("path", ["/fox/v1/validate", "/fox/v1/evaluate"])
def test_oversized_declared_body_is_413_without_upstream_request(
    client: TestClient, upstream: Upstream, path: str
) -> None:
    r = client.post(path, content=b"x" * (FOX_BODY_MAX_BYTES + 1))
    assert r.status_code == 413
    assert set(r.json()) == {"code", "detail"}
    assert upstream.requests == []


def test_oversized_streamed_body_is_413_without_upstream_request(client: TestClient, upstream: Upstream) -> None:
    def chunks() -> Iterator[bytes]:
        for _ in range(FOX_BODY_MAX_BYTES // 1024 + 2):
            yield b"x" * 1024

    r = client.post("/fox/v1/evaluate", content=chunks())
    assert r.status_code == 413
    assert upstream.requests == []


# --- Sec-Fetch-Site --------------------------------------------------------------


@pytest.mark.parametrize("site", ["cross-site", "same-site", "Cross-Site"])
def test_cross_site_request_is_403_without_upstream_request(client: TestClient, upstream: Upstream, site: str) -> None:
    r = client.post("/fox/v1/validate", content=BODY, headers={"Sec-Fetch-Site": site})
    assert r.status_code == 403
    assert set(r.json()) == {"code", "detail"}
    assert upstream.requests == []


@pytest.mark.parametrize("site", [None, "same-origin", "none"])
def test_same_origin_or_absent_fetch_site_is_relayed(client: TestClient, upstream: Upstream, site: str | None) -> None:
    headers = {} if site is None else {"Sec-Fetch-Site": site}
    assert client.post("/fox/v1/evaluate", content=BODY, headers=headers).status_code == 200
    assert len(upstream.requests) == 1


# --- upstream failures -----------------------------------------------------------


@pytest.mark.parametrize("path", ["/fox/v1/validate", "/fox/v1/evaluate"])
def test_timeout_is_504_in_the_envelope_with_the_fox_read_timeout(
    client: TestClient, upstream: Upstream, path: str
) -> None:
    def slow(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    upstream.respond = slow
    r = client.post(path, content=BODY)
    assert r.status_code == 504
    body = r.json()
    assert set(body) == {"code", "detail"}
    assert body["code"] == "formula_eval_timeout"
    [request] = upstream.requests
    assert request.extensions["timeout"]["read"] == FOX_TIMEOUT


def test_transport_error_is_502_in_the_envelope(client: TestClient, upstream: Upstream) -> None:
    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    upstream.respond = down
    r = client.post("/fox/v1/validate", content=BODY)
    assert r.status_code == 502
    assert set(r.json()) == {"code", "detail"}


# --- everything else under /fox is 405 -------------------------------------------


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/fox/v1/formulas"),
        ("POST", "/fox/v1/formulas"),
        ("GET", "/fox/v1/validate"),
        ("GET", "/fox/v1/evaluate"),
        ("PUT", "/fox/v1/validate"),
        ("DELETE", "/fox/v1/evaluate"),
        ("PATCH", "/fox/v1/validate"),
        ("POST", "/fox/v1/validate/extra"),
        ("POST", "/fox/v1/evaluate/"),
        ("POST", "/fox/v2/validate"),
        ("POST", "/fox/v1/ws/formulas"),
        ("POST", "/fox/validate"),
        ("POST", "/fox"),
        ("GET", "/fox"),
    ],
)
def test_other_fox_methods_and_paths_are_405(client: TestClient, upstream: Upstream, method: str, path: str) -> None:
    r = client.request(method, path, content=BODY, headers={"X-API-Key": CLIENT_KEY})
    assert r.status_code == 405
    assert upstream.requests == []


@pytest.mark.parametrize(
    "path",
    [
        "/fox/v1/%2e%2e/v1/validate",
        "/fox/v1/validate/%2e%2e/evaluate",
        "/fox/v1/validate/..%2F..%2F..%2Fhare/v1/pvs",
        "/fox/v1/%252e%252e/%252e%252e/hare",
    ],
)
def test_escaping_or_dotted_paths_never_reach_upstream(client: TestClient, upstream: Upstream, path: str) -> None:
    r = client.post(path, content=BODY)
    assert r.status_code in (404, 405)
    assert upstream.requests == []
