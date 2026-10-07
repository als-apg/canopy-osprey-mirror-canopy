"""The ``screenshot`` tool: render a draft in the server's own viewer and return a PNG.

The draft is loaded at ``/embed/drafts/<id>?<macros>`` on this process (a
library screen, tool id ``library/<id>``, at ``/embed/library/<id>``), so it
renders exactly as the agent's browser would see it, in the pv mode the
server serves in ``config.json``. Before capturing, the page gets one shared,
bounded wait (:data:`SETTLE_TIMEOUT_S`) for two things:

* ``document.fonts.ready``, so text is not captured in a fallback face;
* ``window.__canopy.pvStatus`` reaching a settled value (``connected``,
  ``disconnected`` or ``mock``). The viewer starts it at ``idle`` and moves
  through ``connecting``, so "anything but connecting" would capture a page
  that has not tried to connect yet.

If the wait runs out the screenshot is still taken: a screen whose PVs never
connect is exactly what the agent needs to see. ``pv`` then reports
``disconnected``, as it does for ``mock``: only a live Hare connection is
``connected``. Nothing is written to disk; the PNG goes back inline.

A server running without a browser (see :mod:`canopy_mcp.browser`) answers
every call with a tool error saying screenshots are unavailable.
"""

import struct
from typing import Any, Literal
from urllib.parse import quote, urlencode

from fastmcp.tools import ToolResult
from fastmcp.exceptions import ToolError
from fastmcp.utilities.types import Image

from canopy_mcp.urls import embed_path
from canopy_mcp.store import DRAFTS, DraftStore, DraftNotFound, InvalidDraftId
from canopy_mcp.config import get_settings
from canopy_mcp.server import mcp
from canopy_mcp.browser import Browser, BrowserNotStarted, get_browser
from canopy_mcp.tools._common import route

# One budget for fonts + PV status, so the whole call stays under 5 s.
SETTLE_TIMEOUT_S = 4.0
NAVIGATION_TIMEOUT_MS = 10_000
SCREEN_ROOT_SELECTOR = "[data-testid='screen-root']"
SETTLED_STATUSES = ("connected", "disconnected", "mock")

# Bounded inside the page, so an unsettled page never leaves a pending call behind.
_SETTLE_JS = """
async ({ settled, timeoutMs }) => {
  const deadline = Date.now() + timeoutMs;
  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
  await Promise.race([document.fonts.ready, sleep(timeoutMs)]);
  const done = () => settled.includes(window.__canopy && window.__canopy.pvStatus);
  while (!done() && Date.now() < deadline) await sleep(25);
}
"""
_STATUS_JS = "() => (window.__canopy && window.__canopy.pvStatus) || null"

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"

THEMES = ("light", "dark")
Theme = Literal["light", "dark"]


def embed_url(
    base_url: str,
    draft_id: str,
    macros: dict[str, str] | None = None,
    *,
    theme: Theme | None = None,
    category: str = DRAFTS,
) -> str:
    """The embed URL of ``draft_id`` on ``base_url``, macros as query parameters.

    ``draft_id`` is the id inside its store and ``category`` that store's
    manifest category (``drafts``, or ``library`` for a library screen).

    Pins the viewer's zoom to 100 %: the embed route opens large panels fitted
    to the frame, and a screenshot must capture the panel at its authored size.
    Turns the workspace chrome off (``_canopyChrome=0``) so only the active
    screen renders; it is set after the macros, so no macro can turn it back on.
    ``theme`` (``light`` or ``dark``) is added as ``theme=`` after the macros
    too, so a macro named ``theme`` cannot override it; without it the URL
    carries no theme and the viewer uses its own default.
    """
    if theme is not None and theme not in THEMES:
        raise ValueError(f"theme must be one of {', '.join(THEMES)}, not {theme!r}")
    DraftStore.validate_id(draft_id)
    url = base_url.rstrip("/") + embed_path(quote(draft_id, safe="/"), category)
    query = {"_canopyZoom": "100", **{str(name): str(value) for name, value in (macros or {}).items()}}
    query["_canopyChrome"] = "0"
    if theme is not None:
        query.pop("theme", None)  # re-insert last, replacing any macro of that name
        query["theme"] = theme
    return url + "?" + urlencode(query)


def pv_state(status: str | None) -> str:
    """Map the viewer's ``pvStatus`` onto the tool's ``connected|disconnected``."""
    return "connected" if status == "connected" else "disconnected"


def png_size(data: bytes) -> tuple[int, int]:
    """``(width, height)`` from a PNG's IHDR chunk."""
    if len(data) < 24 or not data.startswith(PNG_SIGNATURE) or data[12:16] != b"IHDR":
        raise ValueError("not a PNG")
    width, height = struct.unpack(">II", data[16:24])
    return width, height


MAX_VIEWPORT = 8192


def viewport_for(size: Any, default: dict[str, int]) -> dict[str, int]:
    """The viewport to render a screen of ``size`` (its ``metadata.size``) in.

    Never smaller than ``default`` on either axis, so a small screen renders
    as before; never larger than :data:`MAX_VIEWPORT`; a missing or malformed
    size is ``default``. A screen wider or taller than the default viewport
    would otherwise be clipped at the viewport edge, since the viewer sizes
    the screen root to the document and the element screenshot cannot reach
    past what was laid out.
    """
    out = dict(default)
    if not isinstance(size, dict):
        return out
    for axis in ("width", "height"):
        value = size.get(axis)
        if isinstance(value, bool) or not isinstance(value, int | float):
            continue
        out[axis] = max(out[axis], min(int(value), MAX_VIEWPORT))
    return out


async def capture(
    browser: Browser,
    draft_id: str,
    macros: dict[str, str] | None = None,
    *,
    size: dict[str, Any] | None = None,
    settle_timeout_s: float = SETTLE_TIMEOUT_S,
    theme: Theme | None = None,
    category: str = DRAFTS,
) -> tuple[bytes, dict[str, Any]]:
    """Render ``draft_id`` of ``category`` and return ``(png, {width, height, pv})``.

    ``size`` is the screen's ``metadata.size``; the page is sized to fit it.
    ``theme`` renders the page in ``light`` or ``dark``; see :func:`embed_url`.
    """
    url = embed_url(browser.base_url, draft_id, macros, theme=theme, category=category)
    async with browser.context() as context:
        page = await context.new_page()
        viewport = viewport_for(size, browser.viewport)
        if viewport != browser.viewport:
            await page.set_viewport_size(viewport)
        response = await page.goto(url, wait_until="load", timeout=NAVIGATION_TIMEOUT_MS)
        if response is not None and not response.ok:
            raise ToolError(f"viewer answered HTTP {response.status} for {url}")
        # Out of time: capture what is there; pv reports the unsettled status.
        await page.evaluate(_SETTLE_JS, {"settled": list(SETTLED_STATUSES), "timeoutMs": int(settle_timeout_s * 1000)})
        status = await page.evaluate(_STATUS_JS)
        root = await page.query_selector(SCREEN_ROOT_SELECTOR)
        data = await root.screenshot(type="png") if root is not None else await page.screenshot(type="png")
    width, height = png_size(data)
    return data, {"width": width, "height": height, "pv": pv_state(status)}


def _draft_size(store: DraftStore, draft_id: str) -> dict[str, Any] | None:
    """``metadata.size`` of ``draft_id`` in ``store``, or ``None`` when it has none or does not exist."""
    try:
        doc, _ = store.read(draft_id)
    except (DraftNotFound, InvalidDraftId, OSError, ValueError):
        return None
    if not isinstance(doc, dict):
        return None
    meta = doc.get("metadata")
    size = meta.get("size") if isinstance(meta, dict) else None
    return size if isinstance(size, dict) else None


def _playwright_errors() -> tuple[type[BaseException], ...]:
    """Playwright's error class, or nothing to catch when it is not installed."""
    try:
        from playwright.async_api import Error
    except ImportError:
        return ()
    return (Error,)


@mcp.tool
async def screenshot(id: str, macros: dict[str, str] | None = None, theme: Theme | None = None) -> ToolResult:
    """Render draft ``id`` (or ``library/<id>``, a shared library screen) in the viewer and return it as a PNG image.

    ``macros`` are passed to the screen as ``$(NAME)`` values. Also returns
    ``{width, height, pv}``; ``pv`` is ``connected`` only when the viewer
    reached live PVs, otherwise ``disconnected`` (including mock mode), so
    blank values in the image may just mean no PV data. Use it after each
    edit to check layout; nothing is saved to disk. ``theme`` (``light`` or
    ``dark``) renders the screen in that color scheme; leave it out for the
    viewer's default.
    """
    if theme is not None and theme not in THEMES:
        raise ToolError(f"theme must be one of {', '.join(THEMES)}, not {theme!r}")
    store, local_id = route(id, get_settings())
    try:
        browser = get_browser()
        data, info = await capture(
            browser, local_id, macros, size=_draft_size(store, local_id), theme=theme, category=store.category
        )
    except InvalidDraftId as exc:
        raise ToolError(str(exc)) from exc
    except BrowserNotStarted as exc:
        raise ToolError(str(exc)) from exc
    except _playwright_errors() as exc:
        raise ToolError(f"screenshot failed: {exc}") from exc
    return ToolResult(content=[Image(data=data, format="png"), info], structured_content=info)
