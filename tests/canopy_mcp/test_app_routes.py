"""The HTTP surface of canopy_mcp: MCP endpoint, proxy guard, viewer, config and drafts."""

import re
import json
from pathlib import Path
from contextlib import asynccontextmanager

import pytest
from starlette.testclient import TestClient

from canopy_mcp.app import McpProxyGuard, create_app
from canopy_mcp.store import DraftStore
from canopy_mcp.access import AccessGate
from canopy_mcp.config import Settings
from canopy_mcp.static import ASSET_CACHE_CONTROL, inject_base, safe_prefix

FIXTURE_DIST = Path(__file__).parent / "fixtures" / "dist"
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


def _screen(title: str) -> dict:
    return {"metadata": {"title": title}, "root": {"id": "root", "type": "absolute-canvas", "children": []}}


@pytest.fixture
def drafts(tmp_path: Path) -> Path:
    root = tmp_path / "drafts"
    (root / "screens" / "bts").mkdir(parents=True)
    (root / "screens" / "bts" / "view.screen.json").write_text(json.dumps(_screen("BTS")))
    (root / "screens" / "bts" / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\nfake")
    # Outside the drafts root: must never be served.
    (tmp_path / "secret.screen.json").write_text('{"secret": true}')
    return root


@pytest.fixture
def settings(drafts: Path) -> Settings:
    return Settings(drafts_dir=drafts, viewer_dist=FIXTURE_DIST, pv_mode="hare", api_key="sekrit-key")


@pytest.fixture
def client(settings: Settings):
    with TestClient(create_app(settings, startup_hooks=[])) as c:
        yield c


def _bases(body: str) -> list[str]:
    return re.findall(r"<base\b[^>]*>", body)


# --- MCP endpoint and proxy guard -----------------------------------------------


def test_post_mcp_initialize_answers_without_redirect(client: TestClient) -> None:
    r = client.post("/mcp", json=INITIALIZE, headers=MCP_HEADERS, follow_redirects=False)
    assert r.status_code == 200
    assert "serverInfo" in r.text


@pytest.mark.parametrize("header", ["x-forwarded-prefix", "x-osprey-terminal-secret", "X-Forwarded-Prefix"])
def test_mcp_refused_when_proxied(client: TestClient, header: str) -> None:
    r = client.post("/mcp", json=INITIALIZE, headers={**MCP_HEADERS, header: "/panel/canopy"})
    assert r.status_code == 403


@pytest.mark.parametrize("method", ["GET", "POST", "DELETE"])
def test_mcp_with_a_trailing_slash_redirects_and_is_never_the_transport(client: TestClient, method: str) -> None:
    r = client.request(method, "/mcp/", json=INITIALIZE, headers=MCP_HEADERS, follow_redirects=False)
    assert r.status_code == 307
    assert r.headers["location"].endswith("/mcp")
    assert "serverInfo" not in r.text


def test_mcp_with_a_trailing_slash_is_refused_when_proxied(client: TestClient) -> None:
    headers = {**MCP_HEADERS, "x-forwarded-prefix": "/panel/canopy"}
    r = client.post("/mcp/", json=INITIALIZE, headers=headers, follow_redirects=False)
    assert r.status_code == 403


def test_without_an_access_token_the_proxy_guard_is_the_outermost_middleware(client: TestClient) -> None:
    classes = [m.cls for m in client.app.user_middleware]
    assert classes[0] is McpProxyGuard
    assert AccessGate not in classes


def test_without_an_access_token_no_route_asks_for_a_credential(client: TestClient) -> None:
    """Walks ``app.routes``: no route answers the credential refusal when none is configured."""
    refused = {}
    sockets = []
    for route in client.app.routes:
        path = re.sub(r"\{[^}]+\}", "x", route.path)
        if "WebSocket" in type(route).__name__:
            sockets.append(path)
            continue
        for method in sorted(route.methods or ["GET", "POST"]):
            r = client.request(method, path, follow_redirects=False)
            if r.status_code == 401:
                refused[f"{method} {path}"] = r.status_code
    assert refused == {}
    assert set(sockets) == {"/hare/v1/ws/pvs", "/fox/v1/ws/formulas"}
    assert len(client.app.routes) > 20


def test_proxy_headers_do_not_block_viewer(client: TestClient) -> None:
    r = client.get("/embed/drafts/bts/view", headers={"x-osprey-terminal-secret": "s"})
    assert r.status_code == 200


# --- index.html and <base href> -------------------------------------------------


@pytest.mark.parametrize("path", ["/", "/s/drafts/bts/view", "/embed/drafts/bts/view"])
def test_index_without_prefix_has_root_base(client: TestClient, path: str) -> None:
    r = client.get(path)
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert _bases(r.text) == ['<base href="/">']
    assert r.text.index("<base") < r.text.index("./assets/index-fixture.js")


def test_index_under_prefix(client: TestClient) -> None:
    r = client.get("/embed/drafts/bts/view", headers={"x-forwarded-prefix": "/panel/canopy/u-1.a~b"})
    assert _bases(r.text) == ['<base href="/panel/canopy/u-1.a~b/">']


@pytest.mark.parametrize("prefix", ['"><script>alert(1)</script>', "/x/", "panel", "/a//b", "/a b", "//evil.example"])
def test_unsafe_prefix_falls_back_to_root(client: TestClient, prefix: str) -> None:
    r = client.get("/", headers={"x-forwarded-prefix": prefix})
    assert _bases(r.text) == ['<base href="/">']
    assert "<script>alert" not in r.text


def test_safe_prefix_rules() -> None:
    assert safe_prefix(None) == ""
    assert safe_prefix("") == ""
    assert safe_prefix("/a/b") == "/a/b"
    assert safe_prefix('"><script>') == ""


def test_inject_base_replaces_existing_base() -> None:
    page = '<html><head><base href="./"><title>x</title></head></html>'
    out = inject_base(page, "/p")
    assert _bases(out) == ['<base href="/p/">']
    assert out.startswith('<html><head><base href="/p/">')


def test_index_missing_dist_is_503(drafts: Path) -> None:
    with TestClient(create_app(Settings(drafts_dir=drafts, viewer_dist=None), startup_hooks=[])) as c:
        assert c.get("/").status_code == 503


# --- assets ------------------------------------------------------------------------


def test_assets_are_immutable(client: TestClient) -> None:
    r = client.get("/assets/index-fixture.js")
    assert r.status_code == 200
    assert r.headers["cache-control"] == ASSET_CACHE_CONTROL
    assert "canopy viewer fixture" in r.text


def test_asset_traversal_is_404(client: TestClient) -> None:
    assert client.get("/assets/%2e%2e/index.html").status_code == 404
    assert client.get("/assets/missing.js").status_code == 404


# --- config.json ---------------------------------------------------------------


def test_config_json(client: TestClient) -> None:
    r = client.get("/config.json")
    assert r.status_code == 200
    body = r.json()
    assert body["pvMode"] == "hare"
    assert body["screensPath"] == "drafts"
    assert isinstance(body["themes"], dict)
    assert body["views"] == {"path": "views"}
    assert set(body) == {"pvMode", "screensPath", "themes", "views", "colorScheme", "backend", "binding"}
    # conftest stubs DATA's summary to "no DATA": both identity fields are null.
    assert body["backend"] is None and body["binding"] is None
    assert "sekrit-key" not in r.text
    assert not any("key" in k.lower() for k in body)


def test_config_json_mock_mode(drafts: Path) -> None:
    s = Settings(drafts_dir=drafts, viewer_dist=FIXTURE_DIST, pv_mode="mock")
    with TestClient(create_app(s, startup_hooks=[])) as c:
        body = c.get("/config.json").json()
        assert body["pvMode"] == "mock"
        assert "backend" not in body and "binding" not in body


# --- drafts ----------------------------------------------------------------------


def test_manifest_lists_drafts_with_etag(client: TestClient, drafts: Path) -> None:
    r = client.get("/drafts/manifest.json")
    assert r.status_code == 200
    assert r.headers["cache-control"] == "no-cache"
    etag = r.headers["etag"]
    screens = r.json()["screens"]
    assert [s["id"] for s in screens] == ["bts/view"]
    assert screens[0]["path"] == "screens/bts/view.screen.json"

    assert client.get("/drafts/manifest.json", headers={"if-none-match": etag}).status_code == 304

    DraftStore(drafts).path_for("bts/other").write_text(json.dumps(_screen("Other")))
    r2 = client.get("/drafts/manifest.json", headers={"if-none-match": etag})
    assert r2.status_code == 200
    assert r2.headers["etag"] != etag


def test_draft_screen_etag_is_rev_and_304(client: TestClient, drafts: Path) -> None:
    store = DraftStore(drafts)
    rev = store.rev("bts/view")
    r = client.get("/drafts/screens/bts/view.screen.json")
    assert r.status_code == 200
    assert r.headers["etag"] == f'"{rev}"'
    assert r.headers["cache-control"] == "no-cache"
    assert r.json()["metadata"]["title"] == "BTS"

    r304 = client.get("/drafts/screens/bts/view.screen.json", headers={"if-none-match": f'"{rev}"'})
    assert r304.status_code == 304
    assert r304.headers["etag"] == f'"{rev}"'
    assert r304.content == b""

    store.path_for("bts/view").write_text(json.dumps(_screen("BTS v2")))
    r2 = client.get("/drafts/screens/bts/view.screen.json", headers={"if-none-match": f'"{rev}"'})
    assert r2.status_code == 200
    assert r2.json()["metadata"]["title"] == "BTS v2"


def test_draft_image_served_with_etag(client: TestClient) -> None:
    r = client.get("/drafts/screens/bts/logo.png")
    assert r.status_code == 200
    assert r.content.startswith(b"\x89PNG")
    assert client.get("/drafts/screens/bts/logo.png", headers={"if-none-match": r.headers["etag"]}).status_code == 304


@pytest.mark.parametrize(
    "path",
    [
        "/drafts/%2e%2e/secret.screen.json",
        "/drafts/screens/..%2f..%2fsecret.screen.json",
        "/drafts/%252e%252e/secret.screen.json",
    ],
)
def test_draft_traversal_is_404(client: TestClient, path: str) -> None:
    r = client.get(path)
    assert r.status_code == 404
    assert "secret" not in r.text


def test_draft_missing_or_disallowed_is_404(client: TestClient, drafts: Path) -> None:
    (drafts / "notes.txt").write_text("x")
    assert client.get("/drafts/screens/nope.screen.json").status_code == 404
    assert client.get("/drafts/notes.txt").status_code == 404


# --- health and lifespan ---------------------------------------------------------


def test_health(client: TestClient) -> None:
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}
    assert list(r.json()) == ["status"]


def test_lifespan_opens_client_and_runs_hooks_in_order(settings: Settings) -> None:
    events: list[str] = []

    def hook(name: str):
        @asynccontextmanager
        async def run(app):
            assert app.state.http is not None and not app.state.http.is_closed
            events.append(f"start {name}")
            yield
            events.append(f"stop {name}")

        return run

    app = create_app(settings, startup_hooks=[hook("a"), hook("b")])
    with TestClient(app) as c:
        assert events == ["start a", "start b"]
        assert c.get("/health").status_code == 200
        http = app.state.http
    assert events == ["start a", "start b", "stop b", "stop a"]
    assert http.is_closed
