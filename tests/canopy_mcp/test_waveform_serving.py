"""Serving the waveform page: route and config entry.

The page is react-trace's third vite entry, ``waveform.html``, shipped in the
same dist as Trace and the correlation page. canopy_mcp serves it at
``/trace/waveform.html`` exactly like the Trace index (same ``<base href>``,
CSP and no-cache), and advertises it in ``config.json`` only when the file is
installed, so a react-trace dist built without the page keeps working
and advertises nothing.
"""

from __future__ import annotations

import re
from pathlib import Path
from collections.abc import Iterator

import pytest
from starlette.testclient import TestClient

from canopy_mcp.app import create_app
from canopy_mcp.config import Settings

TRACE_CSP = (
    "script-src 'self'; object-src 'none'; base-uri 'self'; frame-src 'none'; "
    "worker-src 'none'; form-action 'none'; frame-ancestors 'self'"
)
PREFIX = "/panel/canopy/u-1.a~b"
WAVE_QUERY = "pv=SR01:BPM:WF:X&pv=SR01:BPM:WF:Y"
CORR_QUERY = "x=SR:DCCT&y=BTS:DIAG:BPM:01:POSITION:X&range=-1h"
TRACE_QUERY = "pv=BTS:DIAG:BPM:01:POSITION:X&range=-1h"

PAGE = (
    '<!doctype html><html><head><base href="/"><script type="module" src="./assets/waveform.js"></script>'
    "</head><body>waveform</body></html>"
)


def _bases(body: str) -> list[str]:
    return re.findall(r'<base href="([^"]*)">', body)


def _make_dist(root: Path, *, index: bool = True, waveform: bool = True, correlation: bool = True) -> Path:
    (root / "assets").mkdir(parents=True)
    if index:
        (root / "index.html").write_text(
            '<!doctype html><html><head><script src="./assets/app.js"></script></head><body></body></html>'
        )
    if waveform:
        (root / "waveform.html").write_text(PAGE)
    if correlation:
        (root / "correlation.html").write_text('<script src="./assets/correlation.js"></script>')
    (root / "assets" / "app.js").write_text("console.log('trace')")
    (root / "assets" / "waveform.js").write_text("console.log('wave')")
    return root


def _settings(tmp_path: Path, trace_dist: Path | None, query: str = "") -> Settings:
    drafts = tmp_path / "drafts"
    drafts.mkdir(exist_ok=True)
    return Settings(
        drafts_dir=drafts,
        trace_dist=trace_dist,
        trace_default_query=TRACE_QUERY,
        correlation_default_query=CORR_QUERY,
        waveform_default_query=query,
        pv_mode="hare",
        api_key="k",
    )


def _client(tmp_path: Path, trace_dist: Path | None, query: str = "") -> TestClient:
    app = create_app(_settings(tmp_path, trace_dist, query), startup_hooks=[])
    return TestClient(app, follow_redirects=False)


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    with _client(tmp_path, _make_dist(tmp_path / "trace"), WAVE_QUERY) as c:
        yield c


# --- settings --------------------------------------------------------------------


def test_waveform_query_defaults_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CANOPY_WAVEFORM_DEFAULT_QUERY", raising=False)
    assert Settings().waveform_default_query == ""


def test_waveform_query_read_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CANOPY_WAVEFORM_DEFAULT_QUERY", WAVE_QUERY)
    assert Settings().waveform_default_query == WAVE_QUERY


# --- route -----------------------------------------------------------------------


@pytest.mark.parametrize("method", ["GET", "HEAD"])
def test_page_served_with_trace_base_csp_no_cache(client: TestClient, method: str) -> None:
    r = client.request(method, "/trace/waveform.html")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert r.headers["content-security-policy"] == TRACE_CSP
    assert r.headers["cache-control"] == "no-cache"
    if method == "GET":
        assert _bases(r.text) == ["/trace/"]
        assert "./assets/waveform.js" in r.text
    else:
        assert r.content == b""


@pytest.mark.parametrize("method", ["GET", "HEAD"])
def test_page_honours_forwarded_prefix(client: TestClient, method: str) -> None:
    r = client.request(method, "/trace/waveform.html", headers={"x-forwarded-prefix": PREFIX})
    assert r.status_code == 200
    assert r.headers["content-security-policy"] == TRACE_CSP
    assert r.headers["cache-control"] == "no-cache"
    if method == "GET":
        assert _bases(r.text) == [f"{PREFIX}/trace/"]


@pytest.mark.parametrize("prefix", ['/x"><script>', "//evil.example", "http://evil"])
def test_page_rejects_unsafe_prefix(client: TestClient, prefix: str) -> None:
    r = client.get("/trace/waveform.html", headers={"x-forwarded-prefix": prefix})
    assert _bases(r.text) == ["/trace/"]
    assert "<script>" not in r.text


def test_page_assets_served_from_trace_assets(client: TestClient) -> None:
    assert client.get("/trace/assets/waveform.js").status_code == 200


def test_route_does_not_disturb_neighbours(client: TestClient) -> None:
    assert client.get("/trace/").status_code == 200
    assert client.get("/trace/correlation.html").status_code == 200
    assert client.get("/trace/other.html").status_code == 404
    assert client.get("/trace/waveform.html/").status_code == 404
    assert client.get("/trace").status_code == 404


def test_page_404_without_file(tmp_path: Path) -> None:
    with _client(tmp_path, _make_dist(tmp_path / "trace", waveform=False), WAVE_QUERY) as c:
        assert c.get("/trace/waveform.html").status_code == 404
        assert c.head("/trace/waveform.html").status_code == 404
        assert c.get("/trace/").status_code == 200


def test_page_404_without_trace_dist(tmp_path: Path) -> None:
    with _client(tmp_path, None, WAVE_QUERY) as c:
        assert c.get("/trace/waveform.html").status_code == 404


def test_page_appearing_after_start_is_served_and_advertised(tmp_path: Path) -> None:
    dist = _make_dist(tmp_path / "trace", waveform=False)
    with _client(tmp_path, dist, WAVE_QUERY) as c:
        assert "waveform" not in c.get("/config.json").json()
        (dist / "waveform.html").write_text(PAGE)
        assert c.get("/trace/waveform.html").status_code == 200
        assert c.get("/config.json").json()["waveform"]["path"] == "trace/waveform.html"


# --- config.json -----------------------------------------------------------------


def test_config_advertises_page_with_query(client: TestClient) -> None:
    config = client.get("/config.json").json()
    assert config["waveform"] == {"path": "trace/waveform.html", "query": WAVE_QUERY}
    assert config["correlation"] == {"path": "trace/correlation.html", "query": CORR_QUERY}
    assert config["trace"] == {"path": "trace/", "query": TRACE_QUERY}


def test_config_query_comes_from_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    dist = _make_dist(tmp_path / "trace")
    drafts = tmp_path / "drafts"
    drafts.mkdir()
    monkeypatch.setenv("CANOPY_WAVEFORM_DEFAULT_QUERY", WAVE_QUERY)
    monkeypatch.setenv("CANOPY_TRACE_DIST", str(dist))
    monkeypatch.setenv("CANOPY_DRAFTS_DIR", str(drafts))
    with TestClient(create_app(Settings(), startup_hooks=[])) as c:
        assert c.get("/config.json").json()["waveform"]["query"] == WAVE_QUERY


def test_config_query_empty_by_default(tmp_path: Path) -> None:
    with _client(tmp_path, _make_dist(tmp_path / "trace")) as c:
        assert c.get("/config.json").json()["waveform"] == {"path": "trace/waveform.html", "query": ""}


def test_config_omits_page_without_file(tmp_path: Path) -> None:
    with _client(tmp_path, _make_dist(tmp_path / "trace", waveform=False), WAVE_QUERY) as c:
        config = c.get("/config.json").json()
    assert "waveform" not in config
    assert "trace" in config
    assert "correlation" in config


def test_config_waveform_independent_of_correlation(tmp_path: Path) -> None:
    with _client(tmp_path, _make_dist(tmp_path / "trace", correlation=False), WAVE_QUERY) as c:
        config = c.get("/config.json").json()
    assert config["waveform"] == {"path": "trace/waveform.html", "query": WAVE_QUERY}
    assert "correlation" not in config


def test_config_omits_page_without_trace_dist(tmp_path: Path) -> None:
    with _client(tmp_path, None, WAVE_QUERY) as c:
        config = c.get("/config.json").json()
    assert "waveform" not in config
    assert "trace" not in config
