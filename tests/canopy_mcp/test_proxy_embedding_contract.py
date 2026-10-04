"""The viewer survives OSPREY's panel proxy byte-for-byte.

OSPREY's web terminal proxies panels under ``{outer}/panel/{id}`` and rewrites
root-absolute paths in HTML/JS/CSS responses. The viewer is built with relative
asset URLs and canopy_mcp injects ``<base href>`` itself, so the rewrite must be a
no-op on everything the viewer serves. If it ever is not, the proxy would
double-prefix URLs the ``<base>`` already resolves.

Runs only when ``CANOPY_VIEWER_DIST`` names a built ``apps/viewer/dist``.
"""

import os
import re
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from canopy_mcp.app import create_app
from canopy_mcp.config import Settings

_DIST_ENV = os.environ.get("CANOPY_VIEWER_DIST")

pytestmark = pytest.mark.skipif(not _DIST_ENV, reason="CANOPY_VIEWER_DIST not set; real viewer dist unavailable")

OUTER_PREFIX = "/u/a"
PANEL_ID = "canopy"
FORWARDED_PREFIX = f"{OUTER_PREFIX}/panel/{PANEL_ID}"
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

# --- VERBATIM copy from OSPREY -------------------------------------------------
# Source: OSPREY commit b1c732b4e,
#   src/osprey/interfaces/web_terminal/routes/proxy.py
#   lines 74-94 (_REWRITE_PREFIXES) and 778-812 (_rewrite_content).
# Do not edit; re-copy when OSPREY changes the proxy rewrite.

_REWRITE_PREFIXES = (
    "/static/",
    "/design-system/",
    "/api/",
    "/files/",
    "/health",
    "/checks",
    "/ws/",
    "/assets/",
    "/dashboard/",
    # Tiled's built-in web UI (served under /ui/) references root-absolute
    # /ui/assets/... bundle paths.
    "/ui/",
    # Event dispatcher endpoints. Without these, the dashboard served through
    # /panel/events/* would call `/webhook/...` at the browser origin (the web
    # terminal) instead of going through this proxy — the dispatcher never
    # sees the request and the user gets a 404.
    "/webhook/",
    "/retry/",
    "/trigger/",
)


def _rewrite_content(body: str, panel_id: str, outer_prefix: str = "") -> str:
    """Rewrite root-absolute paths inside string delimiters for proxied content.

    Only touches known prefixes inside ``"``, ``'``, or backtick delimiters.
    CDN URLs (``https://…``), protocol-relative (``//…``), and data URIs
    are unaffected because the pattern requires a delimiter immediately
    before the ``/``.

    Args:
        body: The response body to rewrite.
        panel_id: The panel ID this content was proxied from.
        outer_prefix: The per-user mount prefix (``/u/<user>`` or ``""``, see
            ``compute_url_prefix()``). Prepended so a panel's internal
            assets/APIs resolve under the outer prefix too, not just
            ``/panel/<id>``. Empty prefix ⇒ unchanged (pre-refactor) output.
    """
    prefix = f"{outer_prefix}/panel/{panel_id}"

    for path in _REWRITE_PREFIXES:
        # Match path inside string delimiters: "/static/..." → "/panel/id/static/..."
        # The lookbehind ensures we only match after a quote character.
        body = re.sub(
            r"""(?<=["'`])""" + re.escape(path),
            prefix + path,
            body,
        )

    # Rewrite bare '/api' (without trailing slash) inside delimiters.
    body = re.sub(r"""(?<=["'`])/api(?=["'`])""", f"{prefix}/api", body)

    # Rewrite href="/" to href="/panel/{id}/"
    body = body.replace('href="/"', f'href="{prefix}/"')
    body = body.replace("href='/'", f"href='{prefix}/'")

    return body


# --- end of verbatim copy --------------------------------------------------------


@pytest.fixture(scope="module")
def dist() -> Path:
    path = Path(_DIST_ENV or "")
    if not (path / "index.html").is_file():
        pytest.fail(f"CANOPY_VIEWER_DIST={path} has no index.html; build the viewer first")
    return path


@pytest.fixture
def client(dist: Path, tmp_path: Path):
    drafts = tmp_path / "drafts"
    drafts.mkdir()
    settings = Settings(drafts_dir=drafts, viewer_dist=dist, pv_mode="hare", api_key="k")
    with TestClient(create_app(settings, startup_hooks=[])) as c:
        yield c


def _rewritable_files(dist: Path) -> list[Path]:
    """index.html plus every JS/CSS file: the content types the proxy rewrites."""
    files = [dist / "index.html"]
    files += sorted(p for p in dist.rglob("*") if p.is_file() and p.suffix in {".js", ".mjs", ".css"})
    return files


def test_dist_has_js_and_css(dist: Path) -> None:
    suffixes = {p.suffix for p in _rewritable_files(dist)}
    assert {".html", ".js", ".css"} <= suffixes


def test_rewrite_is_noop_on_every_dist_file(dist: Path) -> None:
    changed = []
    for path in _rewritable_files(dist):
        body = path.read_text(encoding="utf-8")
        if _rewrite_content(body, PANEL_ID, OUTER_PREFIX) != body:
            changed.append(str(path.relative_to(dist)))
    assert changed == []


def test_rewrite_is_noop_on_served_embed_after_base_injection(client: TestClient) -> None:
    r = client.get("/embed/drafts/x", headers={"x-forwarded-prefix": FORWARDED_PREFIX})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert f'<base href="{FORWARDED_PREFIX}/">' in r.text
    assert _rewrite_content(r.text, PANEL_ID, OUTER_PREFIX) == r.text


def test_rewrite_is_noop_on_served_assets(client: TestClient, dist: Path) -> None:
    for path in sorted((dist / "assets").glob("*")):
        if path.suffix not in {".js", ".css"}:
            continue
        r = client.get(f"/assets/{path.name}", headers={"x-forwarded-prefix": FORWARDED_PREFIX})
        assert r.status_code == 200, path.name
        assert _rewrite_content(r.text, PANEL_ID, OUTER_PREFIX) == r.text, path.name


def test_copied_rewrite_does_rewrite_a_root_absolute_path() -> None:
    """Guard against a vacuous no-op: the copy really rewrites what OSPREY rewrites."""
    assert _rewrite_content('src="/assets/a.js"', PANEL_ID, OUTER_PREFIX) == f'src="{FORWARDED_PREFIX}/assets/a.js"'
    assert _rewrite_content('<a href="/">', PANEL_ID, OUTER_PREFIX) == f'<a href="{FORWARDED_PREFIX}/">'


@pytest.mark.parametrize("header", ["x-forwarded-prefix", "x-osprey-terminal-secret"])
def test_mcp_refused_through_the_proxy(client: TestClient, header: str) -> None:
    value = FORWARDED_PREFIX if header == "x-forwarded-prefix" else "s"
    r = client.post("/mcp", json=INITIALIZE, headers={**MCP_HEADERS, header: value})
    assert r.status_code == 403


def test_mcp_is_reachable_through_the_proxy_with_the_access_credential(dist: Path, tmp_path: Path) -> None:
    """Where a credential is configured it replaces the guard: the proxy that adds it is the way in."""
    drafts = tmp_path / "drafts"
    drafts.mkdir()
    token_file = tmp_path / "front.access"
    token_file.write_text("launch-credential\n")
    settings = Settings(drafts_dir=drafts, viewer_dist=dist, pv_mode="hare", api_key="k", access_token_file=token_file)
    headers = {**MCP_HEADERS, "x-forwarded-prefix": FORWARDED_PREFIX}
    with TestClient(create_app(settings, startup_hooks=[])) as c:
        assert c.post("/mcp", json=INITIALIZE, headers=headers).status_code == 401
        r = c.post("/mcp", json=INITIALIZE, headers={**headers, "X-Canopy-Access": "launch-credential"})
        assert r.status_code == 200
        assert "serverInfo" in r.text
        embed = c.get("/embed/drafts/x", headers={**headers, "X-Canopy-Access": "launch-credential"})
        assert embed.status_code == 200
        assert c.get("/embed/drafts/x", headers=headers).status_code == 401
