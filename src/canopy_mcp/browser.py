"""The one headless Chromium the ``screenshot`` tool renders drafts in.

Chromium is launched once, in the app lifespan (:func:`browser_hook`), and
reused for every call: a cold launch costs a second or more, which would eat
most of the screenshot budget. It is launched at startup rather than on first
use so a missing ``playwright install chromium`` fails the server start, not
the agent's first screenshot. Each call gets its own browser context, so no
cookies, storage or open sockets leak from one screenshot into the next.

The browser loads the server's *own* viewer routes, so it needs the address
the server listens on. That lives with the launcher (``--host``/``--port``),
not in ``Settings``, which is why :func:`browser_hook` takes it.
"""

from typing import Any
from contextlib import asynccontextmanager
from collections.abc import Callable, AsyncIterator

from playwright.async_api import Browser as PlaywrightBrowser
from playwright.async_api import Playwright, BrowserContext, async_playwright

DEFAULT_VIEWPORT = {"width": 1280, "height": 800}


class BrowserNotStarted(RuntimeError):
    """The shared Chromium is not running (the startup hook was not entered)."""


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
        """Launch Chromium; a second call while running is a no-op."""
        if self.running:
            return
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
    async def context(self) -> AsyncIterator[BrowserContext]:
        """A fresh, isolated browser context, closed on exit."""
        if not self.running:
            raise BrowserNotStarted("the screenshot browser is not running")
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
        raise BrowserNotStarted("the screenshot browser is not running; it is started with the server")
    return _browser


def browser_hook(base_url: str, **kwargs: Any) -> Callable[[Any], Any]:
    """A startup hook (see ``canopy_mcp.app.STARTUP_HOOKS``) owning the browser.

    Launches Chromium on enter, publishes it as ``app.state.browser`` and as
    the process-wide browser, and closes it on exit.
    """

    @asynccontextmanager
    async def hook(app: Any) -> AsyncIterator[Browser]:
        browser = Browser(base_url, **kwargs)
        await browser.start()
        app.state.browser = browser
        set_browser(browser)
        try:
            yield browser
        finally:
            if _browser is browser:
                set_browser(None)
            await browser.stop()

    return hook
