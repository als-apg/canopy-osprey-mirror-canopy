"""The screenshot tool and the shared Chromium it renders in.

The browser tests need a Chromium from ``playwright install chromium``; without
one they are skipped. They serve a stub viewer build through the real app on a
real port, because Chromium has to fetch the page over HTTP. The stub stands in
for the viewer: it exposes ``window.__canopy.pvStatus`` and a screen root, and
takes the status it settles on from the ``STATUS`` macro so each test can pick it.
"""

import time
import shutil
import socket
import threading
from pathlib import Path
from collections.abc import Iterator

import pytest
import uvicorn
from mcp.types import ImageContent
from fastmcp.exceptions import ToolError

from canopy_mcp.app import create_app
from canopy_mcp.config import Settings
from canopy_mcp.browser import Browser, BrowserNotStarted, get_browser, browser_hook
from canopy_mcp.tools.screenshot import (
    PNG_SIGNATURE,
    SETTLE_TIMEOUT_S,
    capture,
    pv_state,
    png_size,
    embed_url,
    viewport_for,
)

FIXTURE_DIST = Path(__file__).parent / "fixtures" / "dist"
ROOT_W, ROOT_H = 240, 120

STUB_INDEX = f"""<!doctype html>
<html lang="en">
  <head><meta charset="UTF-8" /><title>stub viewer</title></head>
  <body style="margin:0">
    <div id="root"><div data-testid="screen-root" style="width:{ROOT_W}px;height:{ROOT_H}px;background:#08c"></div></div>
    <script>
      window.__canopy = {{ pvStatus: 'idle' }};
      const target = new URLSearchParams(location.search).get('STATUS');
      setTimeout(() => {{ window.__canopy.pvStatus = 'connecting'; }}, 20);
      if (target) setTimeout(() => {{ window.__canopy.pvStatus = target; }}, 150);
    </script>
  </body>
</html>
"""


# --- Pure helpers -------------------------------------------------------------


def test_embed_url_carries_macros_as_query() -> None:
    assert embed_url("http://h:1/", "bts/view") == "http://h:1/embed/drafts/bts/view?_canopyZoom=100"
    assert embed_url("http://h:1", "a", {"P": "SR:C01", "N": "a b&c"}) == (
        "http://h:1/embed/drafts/a?_canopyZoom=100&P=SR%3AC01&N=a+b%26c"
    )


@pytest.mark.parametrize("bad", ["", "../x", "a/../b", "a?b", "/abs", "a//b"])
def test_embed_url_rejects_invalid_ids(bad: str) -> None:
    with pytest.raises(ValueError):
        embed_url("http://h:1", bad)


@pytest.mark.parametrize(
    ("status", "pv"),
    [
        ("connected", "connected"),
        ("disconnected", "disconnected"),
        ("mock", "disconnected"),
        ("connecting", "disconnected"),
        ("idle", "disconnected"),
        (None, "disconnected"),
    ],
)
def test_pv_state(status: str | None, pv: str) -> None:
    assert pv_state(status) == pv


@pytest.mark.parametrize(
    ("size", "expected"),
    [
        (None, {"width": 1280, "height": 800}),
        ({"width": 640, "height": 400}, {"width": 1280, "height": 800}),
        ({"width": 2410, "height": 900}, {"width": 2410, "height": 900}),
        ({"width": 2410}, {"width": 2410, "height": 800}),
        ({"width": 99999, "height": True}, {"width": 8192, "height": 800}),
        ({"width": "wide", "height": 1000.0}, {"width": 1280, "height": 1000}),
    ],
)
def test_viewport_for_fits_the_screen(size, expected) -> None:
    assert viewport_for(size, {"width": 1280, "height": 800}) == expected


def test_png_size_reads_ihdr() -> None:
    header = PNG_SIGNATURE + b"\x00\x00\x00\rIHDR" + (300).to_bytes(4, "big") + (70).to_bytes(4, "big")
    assert png_size(header + b"\x08\x06\x00\x00\x00") == (300, 70)
    with pytest.raises(ValueError):
        png_size(b"GIF89a" + b"\x00" * 30)


def test_get_browser_before_start_raises() -> None:
    with pytest.raises(BrowserNotStarted):
        get_browser()


async def test_tool_without_browser_is_a_tool_error() -> None:
    from canopy_mcp.tools.screenshot import screenshot

    with pytest.raises(ToolError, match="not running"):
        await screenshot("bts/view")


# --- Live Chromium --------------------------------------------------------------


def _chromium_installed() -> bool:
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            return Path(p.chromium.executable_path).exists()
    except Exception:
        return False


@pytest.fixture(scope="module")
def chromium() -> None:
    if not _chromium_installed():
        pytest.skip("chromium not installed: run `playwright install chromium`")


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _dist(root: Path, index: str | None) -> Path:
    """A viewer build: the fixture dist, with ``index.html`` replaced when given."""
    dist = root / "dist"
    shutil.copytree(FIXTURE_DIST, dist)
    if index is not None:
        (dist / "index.html").write_text(index, encoding="utf-8")
    return dist


class _Server:
    """The app on a real port in a background thread."""

    def __init__(self, settings: Settings, hooks: list) -> None:
        self.port = _free_port()
        self.base_url = f"http://127.0.0.1:{self.port}"
        self.app = create_app(settings, startup_hooks=hooks(self.base_url) if callable(hooks) else hooks)
        config = uvicorn.Config(self.app, host="127.0.0.1", port=self.port, log_level="warning", lifespan="on")
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def __enter__(self) -> "_Server":
        self.thread.start()
        deadline = time.monotonic() + 30
        while not self.server.started:
            if not self.thread.is_alive() or time.monotonic() > deadline:
                raise RuntimeError("test server did not start")
            time.sleep(0.02)
        return self

    def __exit__(self, *exc: object) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=30)


def _settings(tmp: Path, index: str | None) -> Settings:
    drafts = tmp / "drafts"
    (drafts / "screens" / "bts").mkdir(parents=True)
    (drafts / "screens" / "bts" / "view.screen.json").write_text(
        '{"metadata": {"title": "BTS"}, "root": {"id": "root", "type": "absolute-canvas", "children": []}}'
    )
    return Settings(drafts_dir=drafts, viewer_dist=_dist(tmp, index), pv_mode="mock")


@pytest.fixture(scope="module")
def stub_server(chromium: None, tmp_path_factory: pytest.TempPathFactory) -> Iterator[_Server]:
    with _Server(_settings(tmp_path_factory.mktemp("stub"), STUB_INDEX), []) as server:
        yield server


@pytest.fixture(scope="module")
def fixture_server(chromium: None, tmp_path_factory: pytest.TempPathFactory) -> Iterator[_Server]:
    """The fixture dist as-is: a page with no ``window.__canopy`` and no screen root."""
    with _Server(_settings(tmp_path_factory.mktemp("fixture"), None), []) as server:
        yield server


@pytest.fixture
async def stub_browser(stub_server: _Server):
    browser = Browser(stub_server.base_url)
    await browser.start()
    try:
        yield browser
    finally:
        await browser.stop()


async def test_stub_page_returns_png_and_pv_disconnected(stub_browser: Browser) -> None:
    data, info = await capture(stub_browser, "bts/view", {"STATUS": "mock"})
    assert data.startswith(PNG_SIGNATURE)
    assert info == {"width": ROOT_W, "height": ROOT_H, "pv": "disconnected"}


async def test_connected_status_is_reported(stub_browser: Browser) -> None:
    _, info = await capture(stub_browser, "bts/view", {"STATUS": "connected"})
    assert info["pv"] == "connected"


async def test_waits_past_idle_and_connecting(stub_browser: Browser) -> None:
    # The stub sits at idle, then connecting, before settling at 150 ms: a
    # capture that accepted anything but "connecting" would report it early.
    _, info = await capture(stub_browser, "bts/view", {"STATUS": "disconnected"})
    assert info["pv"] == "disconnected"
    _, info = await capture(stub_browser, "bts/view", {"STATUS": "connected"})
    assert info["pv"] == "connected"


async def test_unsettled_status_wait_is_bounded(stub_browser: Browser) -> None:
    started = time.monotonic()
    data, info = await capture(stub_browser, "bts/view", settle_timeout_s=0.5)
    elapsed = time.monotonic() - started
    assert data.startswith(PNG_SIGNATURE)
    assert info["pv"] == "disconnected"  # stuck at "connecting"
    assert elapsed < 0.5 + 3.0


def test_default_wait_fits_the_budget() -> None:
    assert SETTLE_TIMEOUT_S <= 4.0


async def test_page_without_canopy_hook_falls_back_to_full_page(fixture_server: _Server) -> None:
    browser = Browser(fixture_server.base_url, viewport={"width": 640, "height": 360})
    await browser.start()
    try:
        data, info = await capture(browser, "bts/view", settle_timeout_s=0.3)
    finally:
        await browser.stop()
    assert data.startswith(PNG_SIGNATURE)
    assert info == {"width": 640, "height": 360, "pv": "disconnected"}


async def test_browser_start_stop_is_idempotent(stub_server: _Server) -> None:
    browser = Browser(stub_server.base_url)
    await browser.start()
    first = browser._browser
    await browser.start()
    assert browser._browser is first and browser.running
    await browser.stop()
    assert not browser.running
    await browser.stop()
    with pytest.raises(BrowserNotStarted):
        async with browser.context():
            pass


async def test_screenshot_tool_over_mcp_with_lifespan_browser(
    chromium: None, tmp_path_factory: pytest.TempPathFactory
) -> None:
    """The hook launches Chromium at startup; the tool answers over MCP with an image."""
    from fastmcp import Client

    settings = _settings(tmp_path_factory.mktemp("mcp"), STUB_INDEX)
    with _Server(settings, lambda base: [browser_hook(base)]) as server:
        browser = server.app.state.browser
        assert browser.running  # launched at startup, before any call
        drafts = settings.drafts_dir
        before = sorted(p.relative_to(drafts) for p in drafts.rglob("*"))
        async with Client(f"{server.base_url}/mcp") as client:
            result = await client.call_tool("screenshot", {"id": "bts/view", "macros": {"STATUS": "mock"}})
        images = [block for block in result.content if isinstance(block, ImageContent)]
        assert len(images) == 1 and images[0].mimeType == "image/png"
        assert result.structured_content == {"width": ROOT_W, "height": ROOT_H, "pv": "disconnected"}
    assert not browser.running
    assert sorted(p.relative_to(drafts) for p in drafts.rglob("*")) == before  # no file written
    with pytest.raises(BrowserNotStarted):
        get_browser()
