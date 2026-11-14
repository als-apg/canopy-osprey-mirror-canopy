"""The e-log under ``/elog/``: relayed to the backend, or answered as a disabled module.

With ``CANOPY_ELOG_RELAY`` on (behind the access credential, with its own write
key), the app's shared ``app.state.http`` client is
swapped, after the lifespan has started, for one built by ``make_client`` over an
``httpx.MockTransport`` whose handler records every upstream request. Off, the
answers are compared with what the e-log module itself returns when disabled.
Nothing reaches a network.
"""

from pathlib import Path
from collections.abc import Callable, Iterator

import httpx
import pytest
from starlette.testclient import TestClient

from canopy_mcp.app import create_app
from canopy_mcp.access import ACCESS_HEADER, CredentialFileError
from canopy_mcp.client import make_client
from canopy_mcp.config import Settings, RelayConfigError, get_settings
from canopy.elog.config import ElogConfig
from canopy.elog.errors import InvalidAppName
from canopy_mcp.forward import ELOG_NOT_RELAYED, ELOG_BODY_MAX_BYTES
from canopy.elog.adapters.registry import ElogRegistry
from canopy.elog.services.elog_service import ElogService

KEY = "server-side-key"
ELOG_WRITE_KEY = "server-elog-write-key"
ACCESS = "access-credential"
CLIENT_KEY = "client-supplied-key"
BASE = "http://canopy-api:8000"

Handler = Callable[[httpx.Request], httpx.Response]


class Upstream:
    """Records upstream requests and answers them with ``self.respond``."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.respond: Handler = lambda request: httpx.Response(
            200, json={"enabled": True}, headers={"content-type": "application/json"}
        )

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.respond(request)


def _secret(tmp_path: Path, name: str, value: str) -> Path:
    path = tmp_path / name
    path.write_text(value + "\n")
    return path


def _settings(tmp_path: Path, *, elog_relay: bool, **overrides: object) -> Settings:
    drafts = tmp_path / "drafts"
    drafts.mkdir(exist_ok=True)
    fields: dict[str, object] = {"base_url": BASE, "api_key": KEY, "drafts_dir": drafts, "elog_relay": elog_relay}
    if elog_relay:
        fields["access_token_file"] = _secret(tmp_path, "access", ACCESS)
        fields["elog_write_api_key_file"] = _secret(tmp_path, "elog.write", ELOG_WRITE_KEY)
    fields.update(overrides)
    return Settings(**fields)


def _client(tmp_path: Path, upstream: Upstream, *, elog_relay: bool) -> Iterator[TestClient]:
    settings = _settings(tmp_path, elog_relay=elog_relay)
    app = create_app(settings, startup_hooks=[])
    with TestClient(app, headers={ACCESS_HEADER: ACCESS} if elog_relay else {}) as c:
        app.state.http = make_client(settings, transport=httpx.MockTransport(upstream))
        yield c


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    for name in ("CANOPY_ELOG_RELAY", "CANOPY_ELOG_WRITE_API_KEY_FILE", "CANOPY_ACCESS_TOKEN_FILE"):
        monkeypatch.delenv(name, raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def upstream() -> Upstream:
    return Upstream()


@pytest.fixture
def relayed(tmp_path: Path, upstream: Upstream) -> Iterator[TestClient]:
    yield from _client(tmp_path, upstream, elog_relay=True)


@pytest.fixture
def not_relayed(tmp_path: Path, upstream: Upstream) -> Iterator[TestClient]:
    yield from _client(tmp_path, upstream, elog_relay=False)


def _disabled_service() -> ElogService:
    return ElogService(ElogConfig.model_construct(), ElogRegistry(None, disabled_reason=ELOG_NOT_RELAYED))


# --- relay off: the disabled module's answers ------------------------------------


def test_config_is_200_disabled_in_the_e_log_modules_own_shape(not_relayed: TestClient, upstream: Upstream) -> None:
    r = not_relayed.get("/elog/v1/apps/trace/config")
    assert r.status_code == 200
    expected = _disabled_service().app_config("trace").model_dump(by_alias=True, mode="json")
    assert r.json() == expected
    assert list(r.json()) == list(expected)
    assert r.json()["enabled"] is False
    assert r.json()["reason"] == ELOG_NOT_RELAYED
    assert upstream.requests == []


@pytest.mark.parametrize("app", ["Trace", "trace!", "a" * 65])
def test_malformed_app_name_is_400_as_the_e_log_module_answers(
    not_relayed: TestClient, upstream: Upstream, app: str
) -> None:
    with pytest.raises(InvalidAppName) as raised:
        _disabled_service().app_config(app)
    for path in (f"/elog/v1/apps/{app}/config", f"/elog/v1/apps/{app}/logbooks"):
        r = not_relayed.get(path)
        assert r.status_code == 400
        assert r.json() == {"code": "invalid_app_name", "detail": raised.value.detail}
    assert upstream.requests == []


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/elog/v1/apps/trace/logbooks"),
        ("GET", "/elog/v1/apps/trace/recent-entries?logbookId=lb"),
        ("POST", "/elog/v1/apps/trace/entries"),
        ("POST", "/elog/v1/apps/trace/config"),
        ("GET", "/elog/v1/health"),
        ("GET", "/elog"),
        ("DELETE", "/elog/anything"),
    ],
)
def test_every_other_e_log_path_is_503_elog_not_configured(
    not_relayed: TestClient, upstream: Upstream, method: str, path: str
) -> None:
    r = not_relayed.request(method, path)
    assert r.status_code == 503
    assert r.json() == {"code": "elog_not_configured", "detail": ELOG_NOT_RELAYED}
    assert upstream.requests == []


# --- relay on --------------------------------------------------------------------


def test_config_is_relayed_with_the_server_key_and_nothing_else_from_the_client(
    relayed: TestClient, upstream: Upstream
) -> None:
    body = b'{"enabled":false,"provider":null,"defaultLogbookIds":[],"reason":"from DATA"}'
    upstream.respond = lambda request: httpx.Response(
        200, content=body, headers={"content-type": "application/json", "x-upstream": "secret"}
    )
    r = relayed.get(
        "/elog/v1/apps/trace/config?api_key=leak",
        headers={"Accept": "application/json", "X-API-Key": CLIENT_KEY, "Cookie": "session=abc"},
    )
    assert r.status_code == 200
    assert r.content == body
    assert "x-upstream" not in r.headers
    [request] = upstream.requests
    assert request.method == "GET"
    assert request.url.raw_path == b"/elog/v1/apps/trace/config"
    assert request.headers.get_list("x-api-key") == [KEY]
    assert request.headers["accept"] == "application/json"
    assert "cookie" not in request.headers


def test_upstream_errors_come_back_verbatim(relayed: TestClient, upstream: Upstream) -> None:
    body = b'{"code":"invalid_app_name","detail":"from DATA"}'
    upstream.respond = lambda request: httpx.Response(400, content=body, headers={"content-type": "application/json"})
    r = relayed.get("/elog/v1/apps/Trace/config")
    assert r.status_code == 400
    assert r.content == body


@pytest.mark.parametrize("suffix", ["entries", "attachments"])
def test_the_two_e_log_writes_are_relayed_with_the_e_log_write_key(
    relayed: TestClient, upstream: Upstream, suffix: str
) -> None:
    payload = b'{"logbookIds":["lb"],"title":"t","bodyMarkdown":"b"}'
    r = relayed.post(
        f"/elog/v1/apps/trace/{suffix}?api_key=leak",
        content=payload,
        headers={
            "Content-Type": "application/json",
            "Sec-Fetch-Site": "same-origin",
            "X-API-Key": CLIENT_KEY,
            "Cookie": "session=abc",
        },
    )
    assert r.status_code == 200
    [request] = upstream.requests
    assert request.method == "POST"
    assert request.url.raw_path == f"/elog/v1/apps/trace/{suffix}".encode()
    assert request.content == payload
    assert request.headers["content-type"] == "application/json"
    assert request.headers.get_list("x-api-key") == [ELOG_WRITE_KEY]
    assert "cookie" not in request.headers


def test_reads_go_with_the_read_key_never_the_write_key(relayed: TestClient, upstream: Upstream) -> None:
    assert relayed.get("/elog/v1/apps/trace/logbooks").status_code == 200
    [request] = upstream.requests
    assert request.headers.get_list("x-api-key") == [KEY]


@pytest.mark.parametrize(
    "path",
    [
        "/elog/v1/apps/trace/config",
        "/elog/v1/apps/trace/logbooks",
        "/elog/v1/apps/trace/entries/extra",
        "/elog/v1/apps/trace/recent-entries",
        "/elog/v1/health",
        "/elog/v1/anything",
    ],
)
def test_a_post_to_any_other_e_log_path_is_405(relayed: TestClient, upstream: Upstream, path: str) -> None:
    r = relayed.post(path, content=b"{}", headers={"Sec-Fetch-Site": "same-origin"})
    assert r.status_code == 405
    assert upstream.requests == []


def test_a_malformed_app_name_on_a_write_is_400_without_upstream_request(
    relayed: TestClient, upstream: Upstream
) -> None:
    r = relayed.post("/elog/v1/apps/Trace!/entries", content=b"{}")
    assert r.status_code == 400
    assert r.json()["code"] == "invalid_app_name"
    assert upstream.requests == []


def test_cross_site_post_is_403_without_upstream_request(relayed: TestClient, upstream: Upstream) -> None:
    r = relayed.post("/elog/v1/apps/trace/entries", content=b"{}", headers={"Sec-Fetch-Site": "cross-site"})
    assert r.status_code == 403
    assert upstream.requests == []


def test_oversized_post_is_413_without_upstream_request(relayed: TestClient, upstream: Upstream) -> None:
    r = relayed.post("/elog/v1/apps/trace/attachments", content=b"x" * (ELOG_BODY_MAX_BYTES + 1))
    assert r.status_code == 413
    assert upstream.requests == []


def test_without_the_access_credential_nothing_is_relayed(relayed: TestClient, upstream: Upstream) -> None:
    for method, path in (("GET", "/elog/v1/apps/trace/config"), ("POST", "/elog/v1/apps/trace/entries")):
        r = relayed.request(method, path, content=b"{}", headers={ACCESS_HEADER: "wrong"})
        assert r.status_code in (401, 403)
    assert upstream.requests == []


@pytest.mark.parametrize("path", ["/elog/v1/%2e%2e/hare/v1/pvs", "/elog/v1/apps/..%2F..%2F..%2Fhare"])
def test_paths_escaping_elog_v1_are_404_without_upstream_request(
    relayed: TestClient, upstream: Upstream, path: str
) -> None:
    assert relayed.get(path).status_code == 404
    assert upstream.requests == []


@pytest.mark.parametrize(("method", "path"), [("PUT", "/elog/v1/apps/trace/config"), ("GET", "/elog")])
def test_other_methods_and_paths_are_405(relayed: TestClient, upstream: Upstream, method: str, path: str) -> None:
    assert relayed.request(method, path).status_code == 405
    assert upstream.requests == []


def test_timeout_is_504_elog_upstream_timeout(relayed: TestClient, upstream: Upstream) -> None:
    def slow(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    upstream.respond = slow
    r = relayed.get("/elog/v1/apps/trace/logbooks")
    assert r.status_code == 504
    assert r.json()["code"] == "elog_upstream_timeout"


def test_transport_error_is_502_elog_upstream_error(relayed: TestClient, upstream: Upstream) -> None:
    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    upstream.respond = down
    r = relayed.get("/elog/v1/apps/trace/config")
    assert r.status_code == 502
    assert r.json()["code"] == "elog_upstream_error"


# --- startup ---------------------------------------------------------------------


def test_relay_on_without_an_e_log_write_key_file_refuses_to_start(tmp_path: Path) -> None:
    with pytest.raises(RelayConfigError, match="CANOPY_ELOG_WRITE_API_KEY_FILE"):
        _settings(tmp_path, elog_relay=True, elog_write_api_key_file=None)


def test_relay_on_without_an_access_credential_refuses_to_start(tmp_path: Path) -> None:
    with pytest.raises(RelayConfigError, match="CANOPY_ACCESS_TOKEN_FILE"):
        _settings(tmp_path, elog_relay=True, access_token_file=None)


def test_relay_on_from_env_without_secrets_refuses_to_start(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CANOPY_ELOG_RELAY", "1")
    with pytest.raises(RelayConfigError):
        Settings()


@pytest.mark.parametrize("content", [None, "", "two tokens"])
def test_an_unreadable_e_log_write_key_refuses_to_start(tmp_path: Path, content: str | None) -> None:
    path = tmp_path / "bad.write"
    if content is not None:
        path.write_text(content)
    with pytest.raises(CredentialFileError, match="CANOPY_ELOG_WRITE_API_KEY_FILE"):
        _settings(tmp_path, elog_relay=True, elog_write_api_key_file=path)


def test_relay_off_needs_no_secret(tmp_path: Path) -> None:
    settings = _settings(tmp_path, elog_relay=False)
    assert settings.elog_write_api_key == ""
