"""Serving the correlation page: route, config entry, and WebSocket compression.

The page is react-trace's second vite entry, ``correlation.html``, shipped in the
same dist as Trace. canopy_mcp serves it at ``/trace/correlation.html`` exactly
like the Trace index (same ``<base href>``, CSP and no-cache), and advertises it
in ``config.json`` only when the file is installed, so a dist built from an
upstream react-trace without the page keeps working and advertises nothing.
"""

from __future__ import annotations

import re
from pathlib import Path
from collections.abc import Iterator

import pytest
from starlette.testclient import TestClient

from canopy_mcp import __main__ as entry
from canopy_mcp.app import create_app
from canopy_mcp.config import Settings

TRACE_CSP = (
    "script-src 'self'; object-src 'none'; base-uri 'self'; frame-src 'none'; "
    "worker-src 'none'; form-action 'none'; frame-ancestors 'self'"
)
PREFIX = "/panel/canopy/u-1.a~b"
CORR_QUERY = "x=SR:DCCT&y=BTS:DIAG:BPM:01:POSITION:X&range=-1h"
TRACE_QUERY = "pv=BTS:DIAG:BPM:01:POSITION:X&range=-1h"

PAGE = (
    '<!doctype html><html><head><base href="/"><script type="module" src="./assets/correlation.js"></script>'
    "</head><body>correlation</body></html>"
)


def _bases(body: str) -> list[str]:
    return re.findall(r'<base href="([^"]*)">', body)


def _make_dist(root: Path, *, index: bool = True, correlation: bool = True) -> Path:
    (root / "assets").mkdir(parents=True)
    if index:
        (root / "index.html").write_text(
            '<!doctype html><html><head><script src="./assets/app.js"></script></head><body></body></html>'
        )
    if correlation:
        (root / "correlation.html").write_text(PAGE)
    (root / "assets" / "app.js").write_text("console.log('trace')")
    (root / "assets" / "correlation.js").write_text("console.log('corr')")
    return root


def _settings(tmp_path: Path, trace_dist: Path | None, query: str = "") -> Settings:
    drafts = tmp_path / "drafts"
    drafts.mkdir(exist_ok=True)
    return Settings(
        drafts_dir=drafts,
        trace_dist=trace_dist,
        trace_default_query=TRACE_QUERY,
        correlation_default_query=query,
        pv_mode="hare",
        api_key="k",
    )


def _client(tmp_path: Path, trace_dist: Path | None, query: str = "") -> TestClient:
    app = create_app(_settings(tmp_path, trace_dist, query), startup_hooks=[])
    return TestClient(app, follow_redirects=False)


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    with _client(tmp_path, _make_dist(tmp_path / "trace"), CORR_QUERY) as c:
        yield c


# --- settings --------------------------------------------------------------------


def test_correlation_query_defaults_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CANOPY_CORRELATION_DEFAULT_QUERY", raising=False)
    assert Settings().correlation_default_query == ""


def test_correlation_query_read_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CANOPY_CORRELATION_DEFAULT_QUERY", CORR_QUERY)
    assert Settings().correlation_default_query == CORR_QUERY


# --- route -----------------------------------------------------------------------


@pytest.mark.parametrize("method", ["GET", "HEAD"])
def test_page_served_with_trace_base_csp_no_cache(client: TestClient, method: str) -> None:
    r = client.request(method, "/trace/correlation.html")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert r.headers["content-security-policy"] == TRACE_CSP
    assert r.headers["cache-control"] == "no-cache"
    if method == "GET":
        assert _bases(r.text) == ["/trace/"]
        assert "./assets/correlation.js" in r.text


def test_page_honours_forwarded_prefix(client: TestClient) -> None:
    r = client.get("/trace/correlation.html", headers={"x-forwarded-prefix": PREFIX})
    assert r.status_code == 200
    assert _bases(r.text) == [f"{PREFIX}/trace/"]


@pytest.mark.parametrize("prefix", ['/x"><script>', "//evil.example", "http://evil"])
def test_page_rejects_unsafe_prefix(client: TestClient, prefix: str) -> None:
    r = client.get("/trace/correlation.html", headers={"x-forwarded-prefix": prefix})
    assert _bases(r.text) == ["/trace/"]
    assert "<script>" not in r.text


def test_page_assets_served_from_trace_assets(client: TestClient) -> None:
    assert client.get("/trace/assets/correlation.js").status_code == 200


def test_other_trace_html_still_404(client: TestClient) -> None:
    assert client.get("/trace/other.html").status_code == 404
    assert client.get("/trace/correlation.html/").status_code == 404


def test_page_404_without_file(tmp_path: Path) -> None:
    with _client(tmp_path, _make_dist(tmp_path / "trace", correlation=False), CORR_QUERY) as c:
        assert c.get("/trace/correlation.html").status_code == 404
        assert c.get("/trace/").status_code == 200


def test_page_404_without_trace_dist(tmp_path: Path) -> None:
    with _client(tmp_path, None, CORR_QUERY) as c:
        assert c.get("/trace/correlation.html").status_code == 404


def test_page_appearing_after_start_is_served_and_advertised(tmp_path: Path) -> None:
    dist = _make_dist(tmp_path / "trace", correlation=False)
    with _client(tmp_path, dist, CORR_QUERY) as c:
        assert "correlation" not in c.get("/config.json").json()
        (dist / "correlation.html").write_text(PAGE)
        assert c.get("/trace/correlation.html").status_code == 200
        assert c.get("/config.json").json()["correlation"]["path"] == "trace/correlation.html"


# --- config.json -----------------------------------------------------------------


def test_config_advertises_page_with_query(client: TestClient) -> None:
    config = client.get("/config.json").json()
    assert config["correlation"] == {"path": "trace/correlation.html", "query": CORR_QUERY}
    assert config["trace"] == {"path": "trace/", "query": TRACE_QUERY}


def test_config_query_comes_from_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    dist = _make_dist(tmp_path / "trace")
    drafts = tmp_path / "drafts"
    drafts.mkdir()
    monkeypatch.setenv("CANOPY_CORRELATION_DEFAULT_QUERY", CORR_QUERY)
    monkeypatch.setenv("CANOPY_TRACE_DIST", str(dist))
    monkeypatch.setenv("CANOPY_DRAFTS_DIR", str(drafts))
    with TestClient(create_app(Settings(), startup_hooks=[])) as c:
        assert c.get("/config.json").json()["correlation"]["query"] == CORR_QUERY


def test_config_query_empty_by_default(tmp_path: Path) -> None:
    with _client(tmp_path, _make_dist(tmp_path / "trace")) as c:
        assert c.get("/config.json").json()["correlation"] == {"path": "trace/correlation.html", "query": ""}


def test_config_omits_page_without_file(tmp_path: Path) -> None:
    with _client(tmp_path, _make_dist(tmp_path / "trace", correlation=False), CORR_QUERY) as c:
        config = c.get("/config.json").json()
    assert "correlation" not in config
    assert "trace" in config


def test_config_omits_page_without_trace_dist(tmp_path: Path) -> None:
    with _client(tmp_path, None, CORR_QUERY) as c:
        config = c.get("/config.json").json()
    assert "correlation" not in config
    assert "trace" not in config


# --- WebSocket compression off --------------------------------------------------


def test_uvicorn_runs_without_per_message_deflate(monkeypatch: pytest.MonkeyPatch) -> None:
    import canopy_mcp.app as app_mod

    calls: list[dict] = []
    monkeypatch.setattr(app_mod, "create_app", lambda *a, **k: object())
    monkeypatch.setattr("uvicorn.run", lambda *a, **k: calls.append(k))
    assert entry.main(["--http", "--port", "9124"]) == 0
    assert len(calls) == 1
    assert calls[0]["ws_per_message_deflate"] is False


def test_relay_does_not_offer_deflate_upstream(tmp_path: Path) -> None:
    import threading

    from websockets.sync.server import ServerConnection, serve

    seen: list[list[str]] = []

    def handle(ws: ServerConnection) -> None:
        seen.append(ws.request.headers.get_all("Sec-WebSocket-Extensions"))
        ws.send("welcome")

    server = serve(handle, "127.0.0.1", 0)
    port = server.socket.getsockname()[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        drafts = tmp_path / "drafts"
        drafts.mkdir()
        settings = Settings(base_url=f"http://127.0.0.1:{port}", drafts_dir=drafts, self_url="http://testserver")
        app = create_app(settings, startup_hooks=[])
        with TestClient(app) as c, c.websocket_connect("/fox/v1/ws/formulas") as ws:
            assert ws.receive_text() == "welcome"
    finally:
        server.shutdown()
        thread.join(timeout=5)
    assert seen == [[]]
