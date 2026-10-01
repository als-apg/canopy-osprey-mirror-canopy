"""The react-trace routes: exactly ``/trace/``, its assets and favicon, nothing else."""

import re
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from canopy_mcp.app import create_app
from canopy_mcp.config import Settings

TRACE_CSP = (
    "script-src 'self'; object-src 'none'; base-uri 'self'; frame-src 'none'; "
    "worker-src 'none'; form-action 'none'; frame-ancestors 'self'"
)
PREFIX = "/panel/canopy/u-1.a~b"
QUERY = "pv=BTS:DIAG:BPM:01:POSITION:X&range=-1h"


def _bases(body: str) -> list[str]:
    return re.findall(r'<base href="([^"]*)">', body)


def _make_dist(root: Path, *, index: bool = True) -> Path:
    (root / "assets").mkdir(parents=True)
    if index:
        (root / "index.html").write_text(
            '<!doctype html><html><head><base href="/"><script src="./assets/app.js"></script></head>'
            "<body></body></html>"
        )
    (root / "assets" / "app.js").write_text("console.log('trace')")
    (root / "favicon.svg").write_text("<svg xmlns='http://www.w3.org/2000/svg'/>")
    return root


@pytest.fixture
def secret(tmp_path: Path) -> Path:
    path = tmp_path / "secret.txt"
    path.write_text("do not serve")
    return path


def _client(tmp_path: Path, trace_dist: Path | None, query: str = "") -> TestClient:
    drafts = tmp_path / "drafts"
    drafts.mkdir(exist_ok=True)
    viewer = tmp_path / "viewer"
    if not viewer.exists():
        (viewer / "assets").mkdir(parents=True)
        (viewer / "index.html").write_text("<html><head></head><body>viewer</body></html>")
    settings = Settings(
        drafts_dir=drafts,
        viewer_dist=viewer,
        trace_dist=trace_dist,
        trace_default_query=query,
        pv_mode="hare",
        api_key="k",
    )
    return TestClient(create_app(settings, startup_hooks=[]), follow_redirects=False)


@pytest.fixture
def client(tmp_path: Path, secret: Path):
    dist = _make_dist(tmp_path / "trace")
    with _client(tmp_path, dist, QUERY) as c:
        yield c


def test_settings_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CANOPY_TRACE_DIST", raising=False)
    monkeypatch.delenv("CANOPY_TRACE_DEFAULT_QUERY", raising=False)
    settings = Settings()
    assert settings.trace_dist is None
    assert settings.trace_default_query == ""


def test_settings_from_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("CANOPY_TRACE_DIST", str(tmp_path))
    monkeypatch.setenv("CANOPY_TRACE_DEFAULT_QUERY", QUERY)
    settings = Settings()
    assert settings.trace_dist == tmp_path
    assert settings.trace_default_query == QUERY


@pytest.mark.parametrize("method", ["GET", "HEAD"])
def test_index_injected_base_csp_no_cache(client: TestClient, method: str) -> None:
    r = client.request(method, "/trace/")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert r.headers["content-security-policy"] == TRACE_CSP
    assert r.headers["cache-control"] == "no-cache"
    if method == "GET":
        assert _bases(r.text) == ["/trace/"]
        assert "./assets/app.js" in r.text


def test_index_honours_forwarded_prefix(client: TestClient) -> None:
    r = client.get("/trace/", headers={"x-forwarded-prefix": PREFIX})
    assert r.status_code == 200
    assert _bases(r.text) == [f"{PREFIX}/trace/"]


@pytest.mark.parametrize("prefix", ['/x"><script>', "//evil.example", "/a b", "http://evil"])
def test_index_rejects_unsafe_prefix(client: TestClient, prefix: str) -> None:
    r = client.get("/trace/", headers={"x-forwarded-prefix": prefix})
    assert r.status_code == 200
    assert _bases(r.text) == ["/trace/"]
    assert "<script>" not in r.text


def test_asset_immutable(client: TestClient) -> None:
    r = client.get("/trace/assets/app.js")
    assert r.status_code == 200
    assert r.text == "console.log('trace')"
    assert r.headers["cache-control"] == "public, max-age=31536000, immutable"


def test_favicon(client: TestClient) -> None:
    r = client.get("/trace/favicon.svg")
    assert r.status_code == 200
    assert "<svg" in r.text


@pytest.mark.parametrize(
    "path",
    [
        "/trace",
        "/trace/index.html",
        "/trace/other",
        "/trace/assets/",
        "/trace/assets/missing.js",
        "/trace/favicon.ico",
        "/trace/sub/",
        "/trace/s/x",
        "/trace/assets/sub/../../index.html",
    ],
)
def test_other_trace_paths_404(client: TestClient, path: str) -> None:
    r = client.get(path)
    assert r.status_code == 404
    assert "location" not in r.headers


@pytest.mark.parametrize(
    "path",
    [
        "/trace/assets/%2e%2e/index.html",
        "/trace/assets/%2e%2e/%2e%2e/secret.txt",
        "/trace/assets/..%2f..%2fsecret.txt",
        "/trace/assets/%252e%252e/%252e%252e/secret.txt",
        "/trace/assets/..%5c..%5csecret.txt",
        "/trace/assets/%00app.js",
    ],
)
def test_asset_traversal_404(client: TestClient, path: str) -> None:
    r = client.get(path)
    assert r.status_code == 404
    assert "do not serve" not in r.text


def test_asset_traversal_plain(client: TestClient) -> None:
    # httpx would normalise a literal "..", so send the raw path directly.
    for raw in ("/trace/assets/../../secret.txt", "/trace/assets/../index.html"):
        r = client.request("GET", "http://testserver" + raw)
        assert r.status_code == 404
        assert "do not serve" not in r.text
    from canopy_mcp.static import _file_under

    root = client.app.state.settings.trace_dist / "assets"
    assert _file_under(root, "../../secret.txt") is None
    assert _file_under(root, "../index.html") is None


def test_asset_symlink_escape_404(tmp_path: Path, secret: Path) -> None:
    dist = _make_dist(tmp_path / "trace")
    (dist / "assets" / "link.js").symlink_to(secret)
    with _client(tmp_path, dist) as c:
        assert c.get("/trace/assets/link.js").status_code == 404


def test_config_carries_trace_when_installed(client: TestClient) -> None:
    body = client.get("/config.json").json()
    assert body["trace"] == {"path": "trace/", "query": QUERY}
    assert {"pvMode", "screensPath", "themes"} <= set(body)


def test_config_trace_query_defaults_empty(tmp_path: Path) -> None:
    dist = _make_dist(tmp_path / "trace")
    with _client(tmp_path, dist) as c:
        assert c.get("/config.json").json()["trace"] == {"path": "trace/", "query": ""}


def test_no_trace_dist(tmp_path: Path) -> None:
    with _client(tmp_path, None, QUERY) as c:
        assert "trace" not in c.get("/config.json").json()
        for path in ("/trace/", "/trace/assets/app.js", "/trace/favicon.svg", "/trace"):
            r = c.get(path)
            assert r.status_code == 404, path
            assert "location" not in r.headers


def test_trace_dist_without_index(tmp_path: Path) -> None:
    dist = _make_dist(tmp_path / "trace", index=False)
    with _client(tmp_path, dist, QUERY) as c:
        assert "trace" not in c.get("/config.json").json()
        assert c.get("/trace/").status_code == 404


def test_viewer_routes_unaffected(client: TestClient) -> None:
    r = client.get("/")
    assert r.status_code == 200
    assert _bases(r.text) == ["/"]
    assert "content-security-policy" not in r.headers
