"""The real viewer: a hand-written draft renders chrome-free and ``screenshot`` answers in < 5 s.

Live: needs ``CANOPY_LIVE=1``, a canopy API (``CANOPY_BASE_URL``, default
``http://127.0.0.1:8000``, key from ``CANOPY_API_KEY`` or the file ``CANOPY_READ_KEY_FILE`` names; the
test is skipped without either) and ``CANOPY_VIEWER_DIST`` naming a built
``apps/viewer/dist``. The test starts its own ``canopy_mcp`` server (the same
``create_app`` the deployment runs) on a free loopback port, serving a copy of
``fixtures/drafts`` -- one hand-written 4-widget screen, ``live/status``.

Two checks, in the order an agent meets them:

* Playwright opens ``/embed/drafts/live/status``: the screen root renders, the
  ``AppHeader`` chrome (a ``<header>`` with an "All screens" link) does not, and
  every request and WebSocket the page opens stays on the server's origin.
* An MCP client calls the ``screenshot`` tool over streamable HTTP: a PNG comes
  back in under 5 s, with ``pv`` reported as ``connected`` or ``disconnected``.

The embed URL is printed (``pytest -s`` shows it) for the manual check.
"""

import os
import sys
import time
import base64
import shutil
import socket
import subprocess
from pathlib import Path
from urllib.parse import urlsplit
from collections.abc import Iterator

import httpx
import pytest
from fastmcp import Client
from mcp.types import ImageContent
from playwright.async_api import async_playwright

pytestmark = pytest.mark.live

HERE = Path(__file__).parent
DRAFTS_FIXTURE = HERE / "fixtures" / "drafts"
DRAFT_ID = "live/status"
HOST = "127.0.0.1"
BUDGET_S = 5.0
STARTUP_TIMEOUT_S = 60.0
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"

# The launcher: the deployed ASGI app, told where it listens so the screenshot
# browser loads this server's own viewer routes.
_LAUNCH = """
import sys, uvicorn
from canopy_mcp.app import create_app
host, port = sys.argv[1], int(sys.argv[2])
uvicorn.run(create_app(self_url=f"http://{host}:{port}"), host=host, port=port, log_level="warning")
"""


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind((HOST, 0))
        return sock.getsockname()[1]


def _api_key() -> str:
    """``CANOPY_API_KEY``, else the key in the file ``CANOPY_READ_KEY_FILE`` names; skip without either."""
    key = os.environ.get("CANOPY_API_KEY")
    if key:
        return key
    key_file = os.environ.get("CANOPY_READ_KEY_FILE")
    if not key_file:
        pytest.skip("live test: set CANOPY_API_KEY or CANOPY_READ_KEY_FILE")
    return Path(key_file).read_text().strip()


def _server_env(drafts: Path) -> dict[str, str]:
    api_key = _api_key()
    dist = os.environ.get("CANOPY_VIEWER_DIST", "")
    if not dist or not (Path(dist) / "index.html").is_file():
        pytest.fail(f"CANOPY_VIEWER_DIST={dist!r} is not a built viewer dist (no index.html)")
    env = dict(os.environ, CANOPY_DRAFTS_DIR=str(drafts), CANOPY_VIEWER_DIST=dist)
    env["CANOPY_API_KEY"] = api_key
    return env


@pytest.fixture(scope="module")
def server(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    """A running canopy_mcp on a free port; yields its base URL."""
    drafts = tmp_path_factory.mktemp("live") / "drafts"
    shutil.copytree(DRAFTS_FIXTURE, drafts)
    port = _free_port()
    base = f"http://{HOST}:{port}"
    log = drafts.parent / "canopy_mcp.log"
    with log.open("wb") as out:
        proc = subprocess.Popen(
            [sys.executable, "-c", _LAUNCH, HOST, str(port)],
            env=_server_env(drafts),
            stdout=out,
            stderr=subprocess.STDOUT,
        )
    try:
        deadline = time.monotonic() + STARTUP_TIMEOUT_S
        while True:
            if proc.poll() is not None:
                pytest.fail(f"canopy_mcp exited with {proc.returncode}:\n{log.read_text()[-3000:]}")
            try:
                if httpx.get(f"{base}/health", timeout=1.0).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            if time.monotonic() > deadline:
                pytest.fail(f"canopy_mcp not healthy within {STARTUP_TIMEOUT_S} s:\n{log.read_text()[-3000:]}")
            time.sleep(0.25)
        yield base
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


def _origin(url: str) -> tuple[str, str]:
    parts = urlsplit(url)
    scheme = {"ws": "http", "wss": "https"}.get(parts.scheme, parts.scheme)
    return scheme, parts.netloc


async def test_embed_renders_without_chrome_and_stays_on_origin(server: str) -> None:
    url = f"{server}/embed/drafts/{DRAFT_ID}"
    print(f"\nembed URL: {url}")
    requests: list[str] = []
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            page = await browser.new_page()
            page.on("request", lambda req: requests.append(req.url))
            page.on("websocket", lambda ws: requests.append(ws.url))
            response = await page.goto(url, wait_until="load")
            assert response is not None and response.ok, f"embed answered {response and response.status}"

            root = page.locator("[data-testid='screen-root']")
            await root.wait_for(state="visible", timeout=10_000)
            await page.get_by_text("Vacuum status").wait_for(timeout=5_000)
            await page.wait_for_load_state("networkidle")

            assert await page.locator("header").count() == 0, "AppHeader chrome rendered on the embed route"
            assert await page.get_by_text("All screens").count() == 0
            assert await page.get_by_text("Page not found").count() == 0
        finally:
            await browser.close()

    here = _origin(server)
    network = [u for u in requests if urlsplit(u).scheme in ("http", "https", "ws", "wss")]
    assert network, "the page made no network requests at all"
    off_origin = [u for u in network if _origin(u) != here]
    assert off_origin == [], f"off-origin requests: {off_origin}"


async def test_screenshot_tool_returns_png_under_five_seconds(server: str) -> None:
    async with Client(f"{server}/mcp") as client:
        start = time.perf_counter()
        result = await client.call_tool("screenshot", {"id": DRAFT_ID})
        elapsed = time.perf_counter() - start

    assert not result.is_error
    images = [c for c in result.content if isinstance(c, ImageContent)]
    assert len(images) == 1 and images[0].mimeType == "image/png"
    assert base64.b64decode(images[0].data).startswith(PNG_SIGNATURE)
    info = result.structured_content or {}
    assert info.get("pv") in ("connected", "disconnected"), info
    assert info.get("width", 0) > 0 and info.get("height", 0) > 0, info
    assert elapsed < BUDGET_S, f"screenshot took {elapsed:.3f} s (budget {BUDGET_S} s)"
