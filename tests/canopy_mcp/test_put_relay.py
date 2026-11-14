"""The opt-in exact PUT relay: ``PUT /hare/v1/pvs/{pv}`` forwarded to the canopy backend.

The relay only runs behind the access credential, so every app here is built
with an access-token file and the test client carries ``X-Canopy-Access``. As
in the Mole tests, the app's shared ``app.state.http`` is swapped, after the
lifespan has started, for a client over an ``httpx.MockTransport`` whose
handler records every upstream request. Nothing reaches a network.

The relay's own refusals and its timeout/transport answers are checked against
the PV-write vector file (``canopy/contracts/write_outcome_vectors.json``), the one source
of their codes and texts.
"""

from pathlib import Path
from collections.abc import Callable, Iterator

import httpx
import pytest
from starlette.routing import Route
from starlette.testclient import TestClient

from canopy_mcp.app import create_app
from canopy_mcp.http import ALLOWED_FETCH_SITES
from canopy.contracts import load_write_outcome_vectors
from canopy_mcp.access import ACCESS_HEADER, CredentialFileError
from canopy_mcp.client import make_client
from canopy_mcp.config import Settings, RelayConfigError, get_settings
from canopy_mcp.forward import (
    PVS_PREFIX,
    MOLE_PREFIX,
    PUT_BODY_MAX_BYTES,
    build_forward_routes,
    contained_upstream_path,
)

READ_KEY = "server-read-key"
WRITE_KEY = "server-write-key"
CLIENT_KEY = "client-supplied-key"
ACCESS = "access-credential"
BASE = "http://canopy-api:8000"
ROWS = load_write_outcome_vectors()["rows"]
BODY = b'{"value": 1.5}'

Handler = Callable[[httpx.Request], httpx.Response]


class Upstream:
    """Records upstream requests (with their bodies) and answers them with ``self.respond``."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.bodies: list[bytes] = []
        self.respond: Handler = lambda request: httpx.Response(
            200,
            json={"pv": "X", "outcome": "confirmed"},
            headers={"content-type": "application/json"},
        )

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        self.bodies.append(request.read())
        return self.respond(request)


def _secret(tmp_path: Path, name: str, value: str) -> Path:
    path = tmp_path / name
    path.write_text(value + "\n")
    return path


def _settings(tmp_path: Path, **overrides: object) -> Settings:
    drafts = tmp_path / "drafts"
    drafts.mkdir(exist_ok=True)
    fields: dict[str, object] = {
        "base_url": BASE,
        "api_key": READ_KEY,
        "drafts_dir": drafts,
        "access_token_file": _secret(tmp_path, "access", ACCESS),
        "put_relay": True,
        "write_api_key_file": _secret(tmp_path, "front.write", WRITE_KEY),
    }
    fields.update(overrides)
    return Settings(**fields)


def _client(settings: Settings, upstream: Upstream) -> Iterator[TestClient]:
    app = create_app(settings, startup_hooks=[])
    with TestClient(app, headers={ACCESS_HEADER: ACCESS}) as c:
        app.state.http = make_client(settings, transport=httpx.MockTransport(upstream))
        yield c


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    for name in (
        "CANOPY_PUT_RELAY",
        "CANOPY_WRITE_API_KEY_FILE",
        "CANOPY_PUT_RELAY_TIMEOUT_S",
        "CANOPY_ACCESS_TOKEN_FILE",
        "CANOPY_API_KEY_FILE",
    ):
        monkeypatch.delenv(name, raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def upstream() -> Upstream:
    return Upstream()


@pytest.fixture
def client(tmp_path: Path, upstream: Upstream) -> Iterator[TestClient]:
    yield from _client(_settings(tmp_path), upstream)


def _assert_row(response: httpx.Response, row: str, status: int | None = None) -> None:
    vector = ROWS[row]
    assert response.status_code == (vector["status"] if status is None else status)
    assert response.json() == {"code": vector["code"], "detail": vector["detail"]}


# --- route table -----------------------------------------------------------------

MUTATING = {"PUT", "POST", "DELETE", "PATCH"}
# POSTs that only compute: the Formula Engine serves both under read access, so they are not write paths.
READ_ONLY_POSTS = {("POST", "/formula-engine/v1/validate"), ("POST", "/formula-engine/v1/evaluate")}


# Endpoints that answer on their own and send nothing upstream.
REFUSALS = {"not_allowed", "elog_not_relayed"}


def _forwarded_mutating_pairs(settings: Settings) -> set[tuple[str, str]]:
    pairs = set()
    for route in build_forward_routes(settings):
        if not isinstance(route, Route) or route.endpoint.__name__ in REFUSALS:
            continue
        for method in route.methods or ():
            if method in MUTATING:
                pairs.add((method, route.path))
    return pairs


@pytest.mark.parametrize("put_relay", [True, False])
def test_the_read_only_formula_engine_posts_are_forwarded_either_way(tmp_path: Path, put_relay: bool) -> None:
    pairs = _forwarded_mutating_pairs(_settings(tmp_path, put_relay=put_relay))
    assert pairs & READ_ONLY_POSTS == READ_ONLY_POSTS


def test_relay_on_forwards_exactly_one_write_pair(tmp_path: Path) -> None:
    pairs = _forwarded_mutating_pairs(_settings(tmp_path)) - READ_ONLY_POSTS
    assert pairs == {("PUT", "/hare/v1/pvs/{pv:path}")}


def test_relay_off_forwards_no_write_pair(tmp_path: Path) -> None:
    assert _forwarded_mutating_pairs(_settings(tmp_path, put_relay=False)) - READ_ONLY_POSTS == set()


def test_the_elog_relay_adds_only_the_two_elog_writes(tmp_path: Path) -> None:
    settings = _settings(
        tmp_path, put_relay=False, elog_relay=True, elog_write_api_key_file=_secret(tmp_path, "elog.write", "k")
    )
    assert _forwarded_mutating_pairs(settings) - READ_ONLY_POSTS == {
        ("POST", "/elog/v1/apps/{app}/entries"),
        ("POST", "/elog/v1/apps/{app}/attachments"),
    }


@pytest.mark.parametrize("method", ["GET", "POST", "DELETE", "PATCH"])
def test_other_methods_on_the_pv_path_stay_405(client: TestClient, upstream: Upstream, method: str) -> None:
    r = client.request(method, "/hare/v1/pvs/SR:C01:MAG:Q1")
    assert r.status_code == 405
    assert upstream.requests == []


@pytest.mark.parametrize(
    "path", ["/hare/v1/pvs", "/hare/v1/ws/pvs", "/hare/v2/pvs/X", "/formula-engine/v1/pvs/X", "/hare/v1/x/X"]
)
def test_put_anywhere_else_stays_405(client: TestClient, upstream: Upstream, path: str) -> None:
    assert client.put(path, content=BODY).status_code == 405
    assert upstream.requests == []


def test_relay_off_put_is_405(tmp_path: Path, upstream: Upstream) -> None:
    for c in _client(_settings(tmp_path, put_relay=False), upstream):
        assert c.put("/hare/v1/pvs/X", content=BODY).status_code == 405
        # The service-prefix catch-all names what is relayed.
        assert "read-only" in c.get("/hare/v1/pvs/X").text
    assert upstream.requests == []


def test_with_the_relay_on_the_405_names_the_relayed_put(client: TestClient, upstream: Upstream) -> None:
    r = client.get("/hare/v1/pvs/X")
    assert r.status_code == 405
    assert "PUT /hare/v1/pvs/<pv>" in r.text and "read-only" not in r.text


# --- the recorded upstream request -----------------------------------------------


def test_only_content_type_and_the_write_key_go_upstream(client: TestClient, upstream: Upstream) -> None:
    r = client.put(
        "/hare/v1/pvs/SR:C01:MAG:Q1",
        content=BODY,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/x-client",
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
    assert request.method == "PUT"
    assert request.url.raw_path == b"/hare/v1/pvs/SR:C01:MAG:Q1"
    assert request.url.host == "canopy-api"
    assert request.headers.get_list("x-api-key") == [WRITE_KEY]
    assert request.headers["content-type"] == "application/json"
    assert upstream.bodies == [BODY]
    for name in ("cookie", "authorization", "origin", "x-custom", "sec-fetch-site", ACCESS_HEADER.lower()):
        assert name not in request.headers
    assert not [name for name in request.headers if name.lower().startswith("x-forwarded")]
    assert request.headers.get("accept") != "application/x-client"
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


def test_no_content_type_from_the_client_sends_none(client: TestClient, upstream: Upstream) -> None:
    assert client.put("/hare/v1/pvs/X", content=BODY).status_code == 200
    [request] = upstream.requests
    assert "content-type" not in request.headers
    assert request.headers.get_list("x-api-key") == [WRITE_KEY]


def test_the_read_key_never_goes_upstream_on_a_write(client: TestClient, upstream: Upstream) -> None:
    client.put("/hare/v1/pvs/X", content=BODY, headers={"Content-Type": "application/json"})
    [request] = upstream.requests
    assert READ_KEY not in request.headers.get_list("x-api-key")


def test_query_string_is_not_forwarded(client: TestClient, upstream: Upstream) -> None:
    assert client.put("/hare/v1/pvs/X?api_key=leak&timeout=1&wait=0", content=BODY).status_code == 200
    [request] = upstream.requests
    assert request.url.raw_path == b"/hare/v1/pvs/X"
    assert request.url.query == b""


@pytest.mark.parametrize(
    ("path", "raw"),
    [
        ("/hare/v1/pvs/ca://SIM:X", b"/hare/v1/pvs/ca://SIM:X"),
        ("/hare/v1/pvs/pva://SIM:Y", b"/hare/v1/pvs/pva://SIM:Y"),
        ("/hare/v1/pvs/A%2FB", b"/hare/v1/pvs/A%2FB"),
        ("/hare/v1/pvs/SR:X.VAL", b"/hare/v1/pvs/SR:X.VAL"),
    ],
)
def test_raw_pv_path_arrives_unchanged(client: TestClient, upstream: Upstream, path: str, raw: bytes) -> None:
    assert client.put(path, content=BODY).status_code == 200
    [request] = upstream.requests
    assert request.url.raw_path == raw


def test_base_url_path_is_prefixed_once(tmp_path: Path, upstream: Upstream) -> None:
    for c in _client(_settings(tmp_path, base_url="http://canopy-api:8000/api/"), upstream):
        assert c.put("/hare/v1/pvs/X", content=BODY).status_code == 200
    [request] = upstream.requests
    assert request.url.raw_path == b"/api/hare/v1/pvs/X"


# --- containment -----------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "/hare/v1/pvs/",
        "/hare/v1/pvs/..%2f..%2fsquirrel/v1/snapshots",
        "/hare/v1/pvs/..%2F",
        "/hare/v1/pvs/%2e%2e",
        "/hare/v1/pvs/%2E%2E/%2E%2E/formula-engine/v1/formulas",
        "/hare/v1/pvs/%252e%252e/%252e%252e/%252e%252e/squirrel",
        "/hare/v1/pvs/%252e%252e",
        "/hare/v1/pvs/X%5c..%5c..",
        "/hare/v1/pvs/X%00",
    ],
)
def test_escaping_or_empty_pv_is_refused_404_without_upstream_request(
    client: TestClient, upstream: Upstream, path: str
) -> None:
    r = client.put(path, content=BODY)
    _assert_row(r, "front-refused-404")
    assert upstream.requests == []


def test_contained_upstream_path_keeps_the_mole_prefix_by_default() -> None:
    assert contained_upstream_path("/mole/v1/archivers", b"/mole/v1/archivers") == "/mole/v1/archivers"
    assert contained_upstream_path("/mole/v1/", b"/mole/v1/") is None
    assert contained_upstream_path("/hare/v1/pvs/X", b"/hare/v1/pvs/X") is None
    assert MOLE_PREFIX == "/mole/v1/"


@pytest.mark.parametrize(
    ("scope_path", "raw", "expected"),
    [
        ("/hare/v1/pvs/X", b"/hare/v1/pvs/X", "/hare/v1/pvs/X"),
        ("/hare/v1/pvs/ca://X", b"/hare/v1/pvs/ca://X", "/hare/v1/pvs/ca://X"),
        ("/hare/v1/pvs/A/B", b"/hare/v1/pvs/A%2FB", "/hare/v1/pvs/A%2FB"),
        ("/hare/v1/pvs/", b"/hare/v1/pvs/", None),
        ("/hare/v1/pvs/../x", b"/hare/v1/pvs/..%2fx", None),
        ("/hare/v1/pvs/%2e%2e", b"/hare/v1/pvs/%252e%252e", None),
        ("/mole/v1/x", b"/mole/v1/x", None),
    ],
)
def test_upstream_path_generalised_to_the_pvs_prefix(scope_path: str, raw: bytes, expected: str | None) -> None:
    assert contained_upstream_path(scope_path, raw, PVS_PREFIX) == expected


# --- body cap --------------------------------------------------------------------


def test_body_at_the_cap_is_forwarded(client: TestClient, upstream: Upstream) -> None:
    body = b"x" * PUT_BODY_MAX_BYTES
    assert client.put("/hare/v1/pvs/X", content=body).status_code == 200
    assert upstream.bodies == [body]
    assert PUT_BODY_MAX_BYTES == 64 * 1024


def test_declared_oversized_body_is_refused_413(client: TestClient, upstream: Upstream) -> None:
    r = client.put("/hare/v1/pvs/X", content=b"x" * (PUT_BODY_MAX_BYTES + 1))
    _assert_row(r, "front-refused-413")
    assert upstream.requests == []


def test_streamed_oversized_body_without_length_is_refused_413(client: TestClient, upstream: Upstream) -> None:
    def chunks() -> Iterator[bytes]:
        for _ in range(17):
            yield b"x" * 4096

    r = client.put("/hare/v1/pvs/X", content=chunks())
    _assert_row(r, "front-refused-413")
    assert upstream.requests == []


# --- Sec-Fetch-Site guard --------------------------------------------------------


def test_the_guard_reuses_the_shared_allowed_set() -> None:
    assert ALLOWED_FETCH_SITES == frozenset({"same-origin", "none"})


@pytest.mark.parametrize("site", [None, "same-origin", "none", "Same-Origin", " none "])
def test_allowed_or_absent_fetch_site_is_forwarded(client: TestClient, upstream: Upstream, site: str | None) -> None:
    headers = {} if site is None else {"Sec-Fetch-Site": site}
    assert client.put("/hare/v1/pvs/X", content=BODY, headers=headers).status_code == 200
    assert len(upstream.requests) == 1


@pytest.mark.parametrize("site", ["same-site", "cross-site", "Cross-Site", "", "bogus"])
def test_other_fetch_site_is_refused_403(client: TestClient, upstream: Upstream, site: str) -> None:
    r = client.put("/hare/v1/pvs/X", content=BODY, headers={"Sec-Fetch-Site": site})
    _assert_row(r, "front-refused-403")
    assert upstream.requests == []


def test_without_the_access_credential_the_gate_refuses_first(tmp_path: Path, upstream: Upstream) -> None:
    app = create_app(_settings(tmp_path), startup_hooks=[])
    with TestClient(app) as c:
        app.state.http = make_client(_settings(tmp_path), transport=httpx.MockTransport(upstream))
        r = c.put("/hare/v1/pvs/X", content=BODY)
    assert r.status_code == ROWS["front-refused-401"]["status"]
    assert r.json()["code"] == ROWS["front-refused-401"]["code"]
    assert upstream.requests == []


# --- upstream answers ------------------------------------------------------------


@pytest.mark.parametrize("status", [200, 403, 409, 502])
def test_upstream_status_body_and_content_type_are_verbatim(
    client: TestClient, upstream: Upstream, status: int
) -> None:
    body = b'{"code": "pv_target_changed", "detail": "the control target changed; nothing written"}'
    upstream.respond = lambda request: httpx.Response(
        status, content=body, headers={"content-type": "application/json", "x-upstream": "secret"}
    )
    r = client.put("/hare/v1/pvs/X", content=BODY, headers={"Content-Type": "application/json"})
    assert r.status_code == status
    assert r.content == body
    assert r.headers["content-type"] == "application/json"
    assert "x-upstream" not in r.headers


def test_timeout_is_504_pv_write_unknown_with_row_13_text(client: TestClient, upstream: Upstream) -> None:
    def slow(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    upstream.respond = slow
    r = client.put("/hare/v1/pvs/X", content=BODY)
    _assert_row(r, "connector-unknown", 504)
    assert r.json()["code"] == "pv_write_unknown"
    [request] = upstream.requests
    assert request.extensions["timeout"]["read"] == 27.0


def test_the_relay_timeout_is_configurable(tmp_path: Path, upstream: Upstream) -> None:
    for c in _client(_settings(tmp_path, put_relay_timeout_s=11), upstream):
        assert c.put("/hare/v1/pvs/X", content=BODY).status_code == 200
    [request] = upstream.requests
    assert request.extensions["timeout"] == {"connect": 11.0, "read": 11.0, "write": 11.0, "pool": 11.0}


def test_transport_error_is_502_pv_write_unknown_with_row_13_text(client: TestClient, upstream: Upstream) -> None:
    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    upstream.respond = down
    r = client.put("/hare/v1/pvs/X", content=BODY)
    _assert_row(r, "connector-unknown", 502)


# --- settings and the start refusal ----------------------------------------------


def test_relay_settings_defaults() -> None:
    s = Settings()
    assert s.put_relay is False
    assert s.write_api_key_file is None
    assert s.write_api_key == ""
    assert s.put_relay_timeout_s == 27.0


def test_relay_settings_read_from_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("CANOPY_PUT_RELAY", "1")
    monkeypatch.setenv("CANOPY_WRITE_API_KEY_FILE", str(_secret(tmp_path, "front.write", WRITE_KEY)))
    monkeypatch.setenv("CANOPY_ACCESS_TOKEN_FILE", str(_secret(tmp_path, "access", ACCESS)))
    monkeypatch.setenv("CANOPY_PUT_RELAY_TIMEOUT_S", "12.5")
    s = Settings()
    assert s.put_relay is True
    assert s.write_api_key == WRITE_KEY
    assert s.put_relay_timeout_s == 12.5


def test_empty_relay_variables_count_as_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("CANOPY_PUT_RELAY", "CANOPY_WRITE_API_KEY_FILE", "CANOPY_PUT_RELAY_TIMEOUT_S"):
        monkeypatch.setenv(name, "")
    s = Settings()
    assert (s.put_relay, s.write_api_key_file, s.put_relay_timeout_s) == (False, None, 27.0)


def test_relay_on_without_a_write_key_file_refuses_to_start(tmp_path: Path) -> None:
    with pytest.raises(RelayConfigError, match="CANOPY_WRITE_API_KEY_FILE"):
        _settings(tmp_path, write_api_key_file=None)


def test_relay_on_without_an_access_credential_refuses_to_start(tmp_path: Path) -> None:
    with pytest.raises(RelayConfigError, match="CANOPY_ACCESS_TOKEN_FILE"):
        _settings(tmp_path, access_token_file=None)


@pytest.mark.parametrize("content", [None, "", "two tokens"])
def test_relay_on_with_an_unreadable_write_key_refuses_to_start(tmp_path: Path, content: str | None) -> None:
    path = tmp_path / "bad.write"  # not the fixture's own "front.write"
    if content is not None:
        path.write_text(content)
    with pytest.raises(CredentialFileError, match="CANOPY_WRITE_API_KEY_FILE"):
        _settings(tmp_path, write_api_key_file=path)


def test_relay_on_from_env_without_secrets_refuses_to_start(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CANOPY_PUT_RELAY", "1")
    with pytest.raises(RelayConfigError):
        Settings()


def test_relay_on_with_an_unreadable_access_file_refuses_to_build(tmp_path: Path) -> None:
    settings = _settings(tmp_path, access_token_file=tmp_path / "missing")
    with pytest.raises(CredentialFileError, match="CANOPY_ACCESS_TOKEN_FILE"):
        create_app(settings, startup_hooks=[])


def test_the_write_key_is_only_ever_read_from_its_file(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CANOPY_WRITE_API_KEY", "from-env")
    assert Settings().write_api_key == ""
