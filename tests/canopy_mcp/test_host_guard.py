"""HostGuard: the DNS-rebinding defence in front of every route.

A page on another site that rebinds its host name to this server's address
reaches it under the attacker's host name, and the browser then treats the
page and the server as one origin, so ``Sec-Fetch-Site`` reads ``same-origin``.
The guard refuses any request whose ``Host`` names neither loopback nor the
host of ``CANOPY_SELF_URL`` / ``CANOPY_PUBLIC_URL``: 421 ``{code, detail}`` on
HTTP, close code 1008 on a WebSocket, before the access gate, the proxy guard
or any route. ``GET /health`` stays open, as it is in front of the access gate.
"""

from pathlib import Path

import pytest
from starlette.testclient import TestClient, WebSocketDenialResponse
from starlette.websockets import WebSocketDisconnect

from canopy_mcp.app import HOST_REFUSAL_CODE, create_app
from canopy_mcp.access import ACCESS_HEADER
from canopy_mcp.config import Settings

REBOUND = "http://rebound.example"
ACCESS = "front-access-credential"


def _settings(tmp_path: Path, **overrides: object) -> Settings:
    drafts = tmp_path / "drafts"
    drafts.mkdir(exist_ok=True)
    fields: dict[str, object] = {"base_url": "http://127.0.0.1:9", "drafts_dir": drafts, "put_relay": False}
    fields.update(overrides)
    return Settings(**fields)


def _gated(tmp_path: Path, **overrides: object) -> Settings:
    token = tmp_path / "access"
    token.write_text(ACCESS + "\n")
    write_key = tmp_path / "write"
    write_key.write_text("write-key\n")
    return _settings(tmp_path, access_token_file=token, put_relay=True, write_api_key_file=write_key, **overrides)


REFUSED_ROUTES = [
    pytest.param("POST", "/mcp", id="mcp"),
    pytest.param("GET", "/drafts/manifest.json", id="draft-read"),
    pytest.param("GET", "/config.json", id="viewer-config"),
    pytest.param("POST", "/fox/v1/evaluate", id="fox-post"),
    pytest.param("PUT", "/hare/v1/pvs/SIM:X", id="put-relay"),
    pytest.param("GET", "/mole/v1/history", id="mole-get"),
    pytest.param("GET", "/no/such/route", id="unrouted"),
    pytest.param("GET", "/collections", id="collections"),
    pytest.param("PUT", "/collections/mine", id="collection-write"),
    pytest.param("PUT", "/views/v", id="saved-view-write"),
]


@pytest.mark.parametrize(("method", "path"), REFUSED_ROUTES)
def test_a_rebound_host_is_refused_on_every_route(tmp_path, method, path):
    with TestClient(create_app(_settings(tmp_path), startup_hooks=[]), base_url=REBOUND) as client:
        r = client.request(method, path, headers={"Sec-Fetch-Site": "same-origin"})
    assert r.status_code == 421
    assert r.json() == {"code": HOST_REFUSAL_CODE, "detail": r.json()["detail"]}
    assert r.json()["detail"]


@pytest.mark.parametrize(("method", "path"), REFUSED_ROUTES)
def test_the_host_guard_refuses_before_the_access_gate(tmp_path, method, path):
    """Even a request carrying the credential is refused under a rebound name."""
    app = create_app(_gated(tmp_path), startup_hooks=[])
    with TestClient(app, base_url=REBOUND, headers={ACCESS_HEADER: ACCESS}) as client:
        r = client.request(method, path)
    assert r.status_code == 421


def test_health_stays_open_under_any_host(tmp_path):
    with TestClient(create_app(_settings(tmp_path), startup_hooks=[]), base_url=REBOUND) as client:
        assert client.get("/health").status_code == 200
        assert client.post("/health").status_code == 421


@pytest.mark.parametrize("host", ["127.0.0.1", "127.0.0.1:8770", "localhost:1", "[::1]:8770"])
def test_a_loopback_host_passes(tmp_path, host):
    with TestClient(create_app(_settings(tmp_path), startup_hooks=[])) as client:
        assert client.get("/config.json", headers={"Host": host}).status_code == 200


def test_the_hosts_of_the_configured_urls_pass(tmp_path):
    settings = _settings(tmp_path, self_url="http://panel.internal:8770", public_url="https://panels.example.org")
    app = create_app(settings, startup_hooks=[])
    with TestClient(app) as client:
        assert client.get("/config.json", headers={"Host": "panel.internal:8770"}).status_code == 200
        assert client.get("/config.json", headers={"Host": "PANELS.example.org"}).status_code == 200
        assert client.get("/config.json", headers={"Host": "rebound.example"}).status_code == 421


def test_the_self_url_given_to_create_app_counts(tmp_path):
    app = create_app(_settings(tmp_path), startup_hooks=[], self_url="http://panel.internal:8770")
    with TestClient(app, base_url="http://panel.internal:8770") as client:
        assert client.get("/config.json").status_code == 200


@pytest.mark.parametrize("host", ["", " ", "bad host:x"])
def test_a_missing_or_unparsable_host_is_refused(tmp_path, host):
    with TestClient(create_app(_settings(tmp_path), startup_hooks=[])) as client:
        assert client.get("/config.json", headers={"Host": host}).status_code == 421


@pytest.mark.parametrize("path", ["/hare/v1/ws/pvs", "/fox/v1/ws/formulas"])
def test_a_rebound_websocket_is_closed_with_1008(tmp_path, path):
    with TestClient(create_app(_settings(tmp_path), startup_hooks=[]), base_url=REBOUND) as client:
        with pytest.raises(WebSocketDisconnect) as excinfo, client.websocket_connect(path):
            pass
    assert excinfo.value.code == 1008


def test_a_rebound_websocket_with_the_credential_is_still_closed(tmp_path):
    app = create_app(_gated(tmp_path), startup_hooks=[])
    with TestClient(app, base_url=REBOUND, headers={ACCESS_HEADER: ACCESS}) as client:
        with pytest.raises((WebSocketDisconnect, WebSocketDenialResponse)) as excinfo:
            with client.websocket_connect("/hare/v1/ws/pvs"):
                pass
    assert getattr(excinfo.value, "code", None) == 1008
