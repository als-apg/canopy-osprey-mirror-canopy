"""The one headless Chromium the ``screenshot`` tool renders drafts in.

Chromium is launched once, in the app lifespan (:func:`browser_hook`), and
reused for every call: a cold launch costs a second or more, which would eat
most of the screenshot budget. Each call gets its own browser context, so no
cookies, storage or open sockets leak from one screenshot into the next.

The browser is optional. ``playwright`` is imported only when a launch is
attempted, and a host without it, or without a ``playwright install
chromium``, still starts the server: the hook logs why there is no browser
and the ``screenshot`` tool answers that screenshots are unavailable.

The browser loads the server's *own* viewer routes, so it needs the address
the server listens on. That lives with the launcher (``--host``/``--port``),
not in ``Settings``, which is why :func:`browser_hook` takes it.
"""

import logging
from typing import TYPE_CHECKING, Any
from contextlib import asynccontextmanager
from collections.abc import Callable, AsyncIterator

if TYPE_CHECKING:
    from playwright.async_api import Browser as PlaywrightBrowser
    from playwright.async_api import Playwright, BrowserContext

logger = logging.getLogger(__name__)

UNAVAILABLE = "screenshot unavailable: the screenshot browser is not running on this server"

DEFAULT_VIEWPORT = {"width": 1280, "height": 800}


class BrowserNotStarted(RuntimeError):
    """The shared Chromium is not running (never started, or it could not be launched)."""


class Browser:
    """A long-lived Chromium plus the base URL of the server it screenshots."""

    def __init__(self, base_url: str, *, viewport: dict[str, int] | None = None):
        self.base_url = base_url.rstrip("/")
        self.viewport = dict(viewport or DEFAULT_VIEWPORT)
        self._playwright: Playwright | None = None
        self._browser: PlaywrightBrowser | None = None

    @property
    def running(self) -> bool:
        return self._browser is not None and self._browser.is_connected()

    async def start(self) -> None:
        """Launch Chromium; a second call while running is a no-op.

        Raises ``ImportError`` without ``playwright`` and Playwright's own
        error without an installed Chromium.
        """
        if self.running:
            return
        from playwright.async_api import async_playwright

        playwright = await async_playwright().start()
        try:
            self._browser = await playwright.chromium.launch(headless=True)
        except BaseException:
            await playwright.stop()
            raise
        self._playwright = playwright

    async def stop(self) -> None:
        """Close Chromium and the Playwright driver; safe to call when stopped."""
        browser, playwright = self._browser, self._playwright
        self._browser = self._playwright = None
        try:
            if browser is not None:
                await browser.close()
        finally:
            if playwright is not None:
                await playwright.stop()

    @asynccontextmanager
    async def context(self) -> AsyncIterator["BrowserContext"]:
        """A fresh, isolated browser context, closed on exit."""
        if not self.running:
            raise BrowserNotStarted(UNAVAILABLE)
        assert self._browser is not None
        context = await self._browser.new_context(viewport=self.viewport, device_scale_factor=1)  # type: ignore[arg-type]
        try:
            yield context
        finally:
            await context.close()


# --- Process-wide browser ---------------------------------------------------

_browser: Browser | None = None


def set_browser(browser: Browser | None) -> None:
    """Install (or, with ``None``, clear) the process-wide browser."""
    global _browser
    _browser = browser


def get_browser() -> Browser:
    """The running process-wide browser, or :class:`BrowserNotStarted`."""
    if _browser is None or not _browser.running:
        raise BrowserNotStarted(UNAVAILABLE)
    return _browser


def browser_hook(base_url: str, **kwargs: Any) -> Callable[[Any], Any]:
    """A startup hook (see ``canopy_mcp.app.STARTUP_HOOKS``) owning the browser.

    Launches Chromium on enter, publishes it as ``app.state.browser`` and as
    the process-wide browser, and closes it on exit. A Chromium that cannot be
    launched does not fail startup: the reason is logged, ``app.state.browser``
    is ``None`` and :func:`get_browser` keeps raising :class:`BrowserNotStarted`.
    """

    @asynccontextmanager
    async def hook(app: Any) -> AsyncIterator[Browser | None]:
        browser = Browser(base_url, **kwargs)
        try:
            await browser.start()
        except Exception as exc:  # no playwright, no Chromium, or a launch that failed
            logger.warning("screenshot browser not started; screenshots are unavailable: %s", exc)
            app.state.browser = None
            yield None
            return
        app.state.browser = browser
        set_browser(browser)
        try:
            yield browser
        finally:
            if _browser is browser:
                set_browser(None)
            await browser.stop()

    return hook
