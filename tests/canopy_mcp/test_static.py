"""Headers on the draft route, and the host stylesheet links on the viewer's entry pages."""

import re
import json
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from canopy_mcp.app import create_app
from canopy_mcp.config import Settings

FIXTURE_DIST = Path(__file__).parent / "fixtures" / "dist"
CSP = "default-src 'none'; sandbox"

# The emitted head tail as a ``{prefix}`` template, one link per line. The shell's
# ``packages/shell/fixtures/host/links.html`` is its mirror (see test_host_links_parity.py).
HOST_LINKS = Path(__file__).parent / "fixtures" / "host_links.html"
HOST_PATHS = ["design-system/css/tokens.css", "static/fonts/fonts.css", "design-system/css/canopy-host.css"]
PREFIX = "/u/alice/panel/canopy"
ENTRY_PAGES = ["/", "/embed/bts/view", "/s/bts/view"]


@pytest.fixture
def client(tmp_path: Path):
    root = tmp_path / "drafts"
    (root / "screens" / "bts").mkdir(parents=True)
    (root / "screens" / "bts" / "view.screen.json").write_text(json.dumps({"metadata": {"title": "BTS"}}))
    (root / "screens" / "bts" / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\nfake")
    (root / "screens" / "bts" / "icon.svg").write_bytes(b"<svg xmlns='http://www.w3.org/2000/svg'/>")
    settings = Settings(drafts_dir=root, viewer_dist=FIXTURE_DIST, pv_mode="hare", api_key="k")
    with TestClient(create_app(settings, startup_hooks=[])) as c:
        yield c


@pytest.mark.parametrize("path", ["/drafts/screens/bts/logo.png", "/drafts/screens/bts/icon.svg"])
def test_image_responses_carry_csp_and_nosniff(client: TestClient, path: str) -> None:
    r = client.get(path)
    assert r.status_code == 200
    assert r.headers["content-security-policy"] == CSP
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["etag"]
    r304 = client.get(path, headers={"if-none-match": r.headers["etag"]})
    assert r304.status_code == 304
    assert r304.headers["content-security-policy"] == CSP
    assert r304.headers["x-content-type-options"] == "nosniff"


def test_screen_responses_carry_no_csp(client: TestClient) -> None:
    r = client.get("/drafts/screens/bts/view.screen.json")
    assert r.status_code == 200
    assert "content-security-policy" not in r.headers
    assert "x-content-type-options" not in r.headers


def _host_client(tmp_path: Path, stylesheets: str) -> TestClient:
    root = tmp_path / "drafts"
    root.mkdir()
    settings = Settings(
        drafts_dir=root, viewer_dist=FIXTURE_DIST, pv_mode="hare", api_key="k", host_stylesheets=stylesheets
    )
    return TestClient(create_app(settings, startup_hooks=[]))


@pytest.fixture
def host_client(tmp_path: Path):
    with _host_client(tmp_path, " ".join(HOST_PATHS)) as c:
        yield c


def _head(body: str) -> str:
    match = re.search(r"<head\b[^>]*>(.*)</head>", body, re.DOTALL)
    assert match is not None
    return match.group(1)


@pytest.mark.parametrize("path", ENTRY_PAGES)
def test_entry_pages_link_the_host_sheets_last_in_head(host_client: TestClient, path: str) -> None:
    r = host_client.get(path, headers={"x-forwarded-prefix": PREFIX})
    assert r.status_code == 200
    head = _head(r.text)
    expected = HOST_LINKS.read_text(encoding="utf-8").replace("{prefix}", PREFIX)
    assert head.endswith(expected)
    assert head.count("data-canopy-host-sheet") == len(HOST_PATHS)
    hrefs = re.findall(r'<link rel="stylesheet" data-canopy-host-sheet href="([^"]*)">', r.text)
    assert hrefs == [f"{PREFIX}/{entry}" for entry in HOST_PATHS]
    # After the bundle's own tags, and nowhere outside <head>.
    assert head.index("index-fixture.js") < head.index("data-canopy-host-sheet")
    assert "data-canopy-host-sheet" not in r.text.split("</head>", 1)[1]
    assert re.search(r"<html data-canopy-host[ >]", r.text)
    assert r.text.count("<base ") == 1


@pytest.mark.parametrize("path", ENTRY_PAGES)
def test_emitted_html_adds_no_script_or_handler(host_client: TestClient, path: str) -> None:
    plain = host_client.get(path).text
    hosted = host_client.get(path, headers={"x-forwarded-prefix": PREFIX}).text
    assert "onerror=" not in hosted
    assert hosted.count("<script") == plain.count("<script") == 1
    assert not re.search(r"<script(?![^>]*\bsrc=)[^>]*>", hosted)
    assert "content-security-policy" not in host_client.get(path, headers={"x-forwarded-prefix": PREFIX}).headers


@pytest.mark.parametrize("path", ENTRY_PAGES)
def test_empty_setting_emits_nothing(tmp_path: Path, path: str) -> None:
    with _host_client(tmp_path, "") as c:
        r = c.get(path, headers={"x-forwarded-prefix": PREFIX})
    assert r.status_code == 200
    assert "data-canopy-host" not in r.text
    assert f'<base href="{PREFIX}/">' in r.text


@pytest.mark.parametrize("prefix", [None, "", "//evil", "//evil.example/x", '/x"><script>', "/a b", "http://evil"])
@pytest.mark.parametrize("path", ENTRY_PAGES)
def test_absent_or_malformed_prefix_emits_nothing(host_client: TestClient, path: str, prefix: str | None) -> None:
    headers = {} if prefix is None else {"x-forwarded-prefix": prefix}
    r = host_client.get(path, headers=headers)
    assert r.status_code == 200
    assert "data-canopy-host" not in r.text
    assert "evil" not in r.text
    assert '<base href="/">' in r.text
