"""The lazy, origin-locked headless Chromium the ``screenshot`` tool renders screens in.

Nothing launches at startup. The lifespan hook (:func:`browser_hook`) only
installs a :class:`Browser` that knows the server's own address and access
credential; the first ``screenshot`` call launches Chromium under the
browser's one lock, and an idle task stops it again :data:`IDLE_TIMEOUT_S`
after the last capture. A host without ``playwright``, without a
``playwright install chromium``, or with a ``websockets`` client that cannot
refuse redirects still starts the server: the launch fails inside the capture
and the tool answers that screenshots are unavailable.

Every capture gets its own browser context, with a route handler and a
WebSocket relay that lock the page to the server's origin: a request to any
other ``(scheme, host, port)`` is aborted, a same-origin request that is not
GET, HEAD or OPTIONS is aborted before any network call, same-origin requests
are fetched with ``X-Canopy-Access`` added for that one request and never
followed through a redirect, and same-origin WebSockets are relayed through
canopy's own ``websockets`` client with the same header, through a ``connect``
that never follows a redirect. No cookie and no context-wide header carry the
credential, so a page cannot read it or send it anywhere else.

``stop()`` bounds every step by :data:`STOP_TIMEOUT_S`; a driver that does not
answer is killed with the descendants snapshotted at launch, because a
container's ``init`` reaps only dead children, and the references are dropped
whatever the kill does, so the next capture launches a fresh Chromium.

**Sandbox.** Chromium runs without its own sandbox unless
``CANOPY_BROWSER_SANDBOX=1``: the sandbox needs user namespaces, which a
default container does not grant, and with it on there Chromium does not
start at all. The page it renders is agent-written, so a host that can grant
them (a desktop, or a container run with the namespaces allowed) should set
the switch. Without it the origin lock and the GET/HEAD/OPTIONS limit above
are what confine the page.

**Playwright internals.** Two helpers reach into Playwright's private objects
(:func:`_tolerate_codeless_close`, :func:`driver_pid`). They were written for
Playwright 1.61 to 1.63; both degrade to doing nothing when the attributes
are gone, and ``tests/canopy_mcp/test_browser_lifecycle.py`` fails loudly when
an upgrade moves them.
"""

import asyncio
import inspect
import logging
from time import monotonic
from typing import TYPE_CHECKING, Any
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from urllib.parse import urlsplit
from collections.abc import Callable, Iterable, Coroutine, AsyncIterator

import psutil
from websockets import ConnectionClosed
from websockets.asyncio.client import ClientConnection, connect

from canopy_mcp.access import ACCESS_HEADER

if TYPE_CHECKING:
    from playwright.async_api import Route
    from playwright.async_api import Browser as PlaywrightBrowser
    from playwright.async_api import Request, Playwright, BrowserContext, WebSocketRoute

logger = logging.getLogger(__name__)

UNAVAILABLE = "screenshot unavailable: the screenshot browser cannot run on this server"
SANDBOX_SWITCH = "CANOPY_BROWSER_SANDBOX"

DEFAULT_VIEWPORT = {"width": 1280, "height": 800}
# Seconds after the last capture at which the idle task stops Chromium.
IDLE_TIMEOUT_S = 300.0
# Bound on each step of ``stop()`` (``browser.close()``, then ``playwright.stop()``).
STOP_TIMEOUT_S = 10.0
# Bound on the relay's upstream handshake.
WS_OPEN_TIMEOUT_S = 10.0

ALLOWED_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
WS_POLICY_VIOLATION = 1008
WS_INTERNAL_ERROR = 1011
# Close codes that describe a closure but may not be sent in a close frame.
_UNSENDABLE_CLOSE_CODES = frozenset({1005, 1006, 1015})

_WS_TO_HTTP = {"ws": "http", "wss": "https"}
_DEFAULT_PORTS = {"http": 80, "https": 443}
_ACCESS_HEADER_LOWER = ACCESS_HEADER.lower()

Origin = tuple[str, str, int]


class BrowserNotStarted(RuntimeError):
    """No screenshot browser can run here: none is installed, or the launch failed."""


# --- Origins ------------------------------------------------------------------------


def origin_of(url: str) -> Origin | None:
    """``(scheme, host, port)`` of ``url``, ws/wss mapped onto http/https; ``None`` for a URL without a clean origin.

    A URL with userinfo (``http://host:port@evil/``, ``http://user@host/``), an
    out-of-range port (``:80001``), an unknown scheme or no host has no origin
    this module compares against, so it never counts as same-origin.
    """
    parts = urlsplit(url)
    scheme = _WS_TO_HTTP.get(parts.scheme.lower(), parts.scheme.lower())
    if scheme not in _DEFAULT_PORTS or "@" in parts.netloc or not parts.hostname:
        return None
    try:
        port = parts.port
    except ValueError:
        return None
    return (scheme, parts.hostname, _DEFAULT_PORTS[scheme] if port is None else port)


def same_origin(url: str, origin: Origin | None) -> bool:
    """Whether ``url`` has exactly ``origin``; never true for a ``None`` origin."""
    return origin is not None and origin_of(url) == origin


# --- WebSocket client ---------------------------------------------------------------


class _RedirectRefusingConnect(connect):
    """``connect`` that never follows a redirect, so the credential never leaves the origin.

    ``process_redirect`` returning the exception unchanged makes the client
    re-raise the redirect status instead of opening a second handshake.
    """

    def process_redirect(self, exc: Exception) -> Exception | str:
        return exc


def redirect_refusal_supported() -> bool:
    """Whether the installed ``websockets`` client has the redirect hook (``>=14``)."""
    return callable(getattr(connect, "process_redirect", None))


def _sendable(code: int | None, default: int) -> int:
    return default if code is None or code in _UNSENDABLE_CLOSE_CODES else code


# websockets >= 15 reads HTTP(S)_PROXY / WS(S)_PROXY from the environment unless told not to;
# the relay must reach the server's own address directly, never a proxy that would see the header.
_CONNECT_PROXY_KW: dict[str, Any] = {"proxy": None} if "proxy" in inspect.signature(connect.__init__).parameters else {}


def _brief(exc: BaseException, secret: str | None = None) -> str:
    """The first line of ``exc``, with ``secret`` redacted: Playwright's call logs repeat request headers."""
    text = str(exc).strip()
    first = text.splitlines()[0] if text else type(exc).__name__
    return first.replace(secret, "<redacted>") if secret else first


@dataclass(frozen=True)
class _PageClosed:
    code: int | None
    reason: str | None


def _tolerate_codeless_close(ws: "WebSocketRoute") -> None:
    """Work around Playwright (1.61 to 1.63) reading ``event["code"]`` on a page-close event that carries none.

    Once a ``WebSocketRoute`` has an ``on_close`` handler, every closure of
    the page's socket reaches ``_channel_close_page``, which indexes ``code``
    and ``reason`` that Chromium does not always send; pyee then prints a
    traceback for every relayed socket. The listener is swapped for one that
    defaults both. Guarded on every private attribute, so a Playwright that
    changed shape is left alone (and keeps its own behaviour).
    """
    impl = getattr(ws, "_impl_obj", None)
    channel = getattr(impl, "_channel", None)
    original = getattr(impl, "_channel_close_page", None)
    if channel is None or original is None:
        return

    def tolerant(event: dict[str, Any]) -> None:
        event.setdefault("code", None)
        event.setdefault("reason", None)
        original(event)

    try:
        channel.remove_listener("closePage", original)
        channel.on("closePage", tolerant)
    except Exception as exc:  # a different emitter than expected: leave Playwright's listener in place
        logger.debug("screenshot browser: could not install the tolerant close listener: %s", exc)


# --- Driver process -----------------------------------------------------------------


def driver_pid(playwright: Any) -> int | None:
    """The pid of the Playwright driver behind ``playwright``, or ``None`` when it cannot be found.

    Read through private attributes (Playwright 1.61 to 1.63); a test pins them.
    """
    try:
        return int(playwright._impl_obj._connection._transport._proc.pid)
    except (AttributeError, TypeError, ValueError):
        return None


def driver_descendants(pid: int | None) -> list[psutil.Process]:
    """The driver's descendants right now (the snapshot ``stop()`` kills on a timeout)."""
    if pid is None:
        return []
    try:
        return psutil.Process(pid).children(recursive=True)
    except psutil.Error:
        return []


def kill_process_tree(pid: int, descendants: Iterable[psutil.Process]) -> None:
    """Kill the process ``pid`` and the given descendants, then wait briefly for them to go."""
    targets: list[psutil.Process] = []
    try:
        targets.append(psutil.Process(pid))
    except psutil.NoSuchProcess:
        pass  # the driver is already gone; its descendants may not be
    targets.extend(descendants)
    killed: list[psutil.Process] = []
    for proc in targets:
        try:
            proc.kill()
            killed.append(proc)
        except psutil.NoSuchProcess:
            continue
    psutil.wait_procs(killed, timeout=3)


async def _bounded(step: str, coroutine: Coroutine[Any, Any, Any]) -> bool:
    """Await one stop step within :data:`STOP_TIMEOUT_S`; ``False`` only when it timed out."""
    try:
        await asyncio.wait_for(coroutine, STOP_TIMEOUT_S)
    except TimeoutError:
        logger.warning("screenshot browser: %s did not finish within %.0f s; killing the driver", step, STOP_TIMEOUT_S)
        return False
    except Exception as exc:
        logger.warning("screenshot browser: %s failed: %s", step, exc)
    return True


async def _launch_chromium(playwright: "Playwright", *, sandbox: bool) -> "PlaywrightBrowser":
    """Launch headless Chromium; the sandbox switch is passed only when it is on."""
    options: dict[str, Any] = {"headless": True}
    if sandbox:
        options["chromium_sandbox"] = True
    return await playwright.chromium.launch(**options)


# --- One capture's routing --------------------------------------------------------


class _Session:
    """The route handler, WebSocket relay and their tasks for one browser context."""

    def __init__(self, origin: Origin | None, access_token: str | None) -> None:
        self.origin = origin
        self.access_token = access_token
        # (method, origin, path, decision): never the query, never a header.
        self.records: list[tuple[str, str, str, str]] = []
        self.tasks: set[asyncio.Task[Any]] = set()
        self.upstreams: set[ClientConnection] = set()
        # Relayed page sockets still open; closed first at teardown, before the context.
        self.sockets: set[WebSocketRoute] = set()

    def _record(self, method: str, url: str, decision: str) -> None:
        origin = origin_of(url)
        shown = f"{origin[0]}://{origin[1]}:{origin[2]}" if origin is not None else "<no origin>"
        self.records.append((method, shown, urlsplit(url).path, decision))
        logger.debug("screenshot browser: %s %s%s -> %s", method, shown, urlsplit(url).path, decision)

    def _brief(self, exc: BaseException) -> str:
        return _brief(exc, self.access_token)

    def _spawn(self, coroutine: Coroutine[Any, Any, Any]) -> asyncio.Task[Any]:
        task = asyncio.create_task(coroutine)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return task

    # HTTP

    async def route(self, route: "Route", request: "Request") -> None:
        try:
            await self._route(route, request)
        except Exception as exc:  # the page is gone, or Playwright refused a late answer
            logger.debug("screenshot browser: route handler for %s ended early: %s", request.method, self._brief(exc))

    async def _route(self, route: "Route", request: "Request") -> None:
        method = request.method.upper()

        async def abort(why: str) -> None:
            self._record(method, request.url, f"aborted: {why}")
            await route.abort()

        if not same_origin(request.url, self.origin):
            await abort("off-origin")
            return
        if method not in ALLOWED_METHODS:
            await abort("method")
            return
        headers = {name: value for name, value in request.headers.items() if name.lower() != _ACCESS_HEADER_LOWER}
        if self.access_token is not None:
            headers[ACCESS_HEADER] = self.access_token
        try:
            response = await route.fetch(max_redirects=0, headers=headers)
        except Exception as exc:
            logger.debug("screenshot browser: fetch failed: %s", self._brief(exc))
            await abort("fetch failed")
            return
        if response.status in REDIRECT_STATUSES and any(name.lower() == "location" for name in response.headers):
            await abort(f"redirect {response.status}")
            return
        self._record(method, request.url, f"fulfilled {response.status}")
        await route.fulfill(response=response)

    # WebSocket

    async def route_web_socket(self, ws: "WebSocketRoute") -> None:
        if not same_origin(ws.url, self.origin):
            self._record("WS", ws.url, "closed: off-origin")
            await self._close_page_socket(ws, WS_POLICY_VIOLATION, "off-origin")
            return
        self._record("WS", ws.url, "relayed")
        from_page: asyncio.Queue[str | bytes | _PageClosed] = asyncio.Queue()
        self.sockets.add(ws)

        def page_closed(code: int | None, reason: str | None) -> None:
            self.sockets.discard(ws)
            from_page.put_nowait(_PageClosed(code, reason))

        _tolerate_codeless_close(ws)
        ws.on_message(from_page.put_nowait)
        ws.on_close(page_closed)
        self._spawn(self._relay(ws, from_page))

    async def _relay(self, ws: "WebSocketRoute", from_page: asyncio.Queue[str | bytes | _PageClosed]) -> None:
        headers = {ACCESS_HEADER: self.access_token} if self.access_token is not None else {}
        try:
            upstream = await _RedirectRefusingConnect(
                ws.url,
                additional_headers=headers,
                open_timeout=WS_OPEN_TIMEOUT_S,
                max_size=None,
                user_agent_header=None,
                compression=None,
                **_CONNECT_PROXY_KW,
            )
        except Exception as exc:  # refused, redirected, timed out, unreachable
            logger.debug("screenshot browser: upstream WebSocket handshake failed: %s", self._brief(exc))
            await self._close_page_socket(ws, WS_INTERNAL_ERROR, "upstream handshake failed")
            return
        self.upstreams.add(upstream)
        self._spawn(self._pump_to_upstream(ws, upstream, from_page))
        self._spawn(self._pump_to_page(ws, upstream))

    async def _pump_to_upstream(
        self, ws: "WebSocketRoute", upstream: ClientConnection, from_page: asyncio.Queue[str | bytes | _PageClosed]
    ) -> None:
        try:
            while True:
                item = await from_page.get()
                if isinstance(item, _PageClosed):
                    await upstream.close(_sendable(item.code, 1000), item.reason or "")
                    return
                await upstream.send(item)
        except ConnectionClosed:
            return  # the other pump reports the upstream's closure to the page
        except Exception as exc:
            logger.debug("screenshot browser: relay to upstream failed: %s", self._brief(exc))
            await self._close_page_socket(ws, WS_INTERNAL_ERROR, "relay error")

    async def _pump_to_page(self, ws: "WebSocketRoute", upstream: ClientConnection) -> None:
        try:
            async for frame in upstream:
                ws.send(frame)
        except ConnectionClosed:
            pass  # how the upstream ended is in its close code, below
        except Exception as exc:  # a page that is gone, or a frame it refused
            logger.debug("screenshot browser: relay to page failed: %s", self._brief(exc))
            await self._close_page_socket(ws, WS_INTERNAL_ERROR, "relay error")
            return
        code = upstream.close_code
        if code is None or code in _UNSENDABLE_CLOSE_CODES:
            # No close frame: the upstream connection was lost, which is an error to the page.
            await self._close_page_socket(ws, WS_INTERNAL_ERROR, "upstream connection lost")
            return
        await self._close_page_socket(ws, code, upstream.close_reason or "")

    async def _close_page_socket(self, ws: "WebSocketRoute", code: int, reason: str) -> None:
        self.sockets.discard(ws)
        try:
            await ws.close(code=code, reason=reason)
        except Exception as exc:  # already closed with the page
            logger.debug("screenshot browser: page socket close (%s) failed: %s", code, self._brief(exc))

    async def teardown(self) -> None:
        """Close every relayed page socket, cancel every pump and close every upstream socket.

        The page sockets go first: a context closed over an open routed socket
        makes Playwright 1.63 report the closure without a code and trip over
        its own listener.
        """
        for ws in list(self.sockets):
            await self._close_page_socket(ws, 1001, "capture finished")
        for task in list(self.tasks):
            task.cancel()
        if self.tasks:
            await asyncio.gather(*list(self.tasks), return_exceptions=True)
        for upstream in list(self.upstreams):
            try:
                await asyncio.wait_for(upstream.close(), STOP_TIMEOUT_S)
            except Exception as exc:
                logger.debug("screenshot browser: upstream close failed: %s", self._brief(exc))
        self.upstreams.clear()


# --- The browser --------------------------------------------------------------------


class Browser:
    """A lazily launched Chromium, locked to the origin of ``base_url``.

    ``access_token`` is added as ``X-Canopy-Access`` to every same-origin
    request and WebSocket handshake the page makes; ``sandbox`` passes
    ``chromium_sandbox=True`` to the launch (``CANOPY_BROWSER_SANDBOX=1``).
    One ``asyncio.Lock`` covers launch, crash recovery, every capture, the
    idle stop and the lifespan exit.
    """

    def __init__(
        self,
        base_url: str,
        *,
        viewport: dict[str, int] | None = None,
        access_token: str | None = None,
        sandbox: bool = False,
        idle_timeout_s: float = IDLE_TIMEOUT_S,
    ):
        self.base_url = base_url.rstrip("/")
        self.origin = origin_of(self.base_url)
        self.viewport = dict(viewport or DEFAULT_VIEWPORT)
        self.access_token = access_token
        self.sandbox = sandbox
        self.idle_timeout_s = idle_timeout_s
        self.lock = asyncio.Lock()
        # The routing record of the latest capture, read only by tests.
        self._last_session_for_tests: _Session | None = None
        self._playwright: Playwright | None = None
        self._browser: PlaywrightBrowser | None = None
        self._driver_pid: int | None = None
        self._driver_children: list[psutil.Process] = []
        self._idle_task: asyncio.Task[None] | None = None
        self._last_used = monotonic()

    @property
    def running(self) -> bool:
        return self._browser is not None and self._browser.is_connected()

    async def start(self) -> None:
        """Launch Chromium now (the first capture would otherwise); a no-op while it runs.

        Raises :class:`BrowserNotStarted` when no browser can run here.
        """
        async with self.lock:
            await self._ensure_started()

    async def shutdown(self) -> None:
        """The lifespan exit: stop under the lock, after any capture in flight."""
        async with self.lock:
            await self.stop()

    async def _ensure_started(self) -> None:
        if self.running:
            return
        if self._browser is not None or self._playwright is not None:
            logger.warning("screenshot browser: Chromium is gone; stopping its driver and relaunching")
            await self.stop()
        await self._launch()

    def _unavailable(self, reason: str, cause: BaseException | None) -> BrowserNotStarted:
        logger.warning("screenshot browser could not be launched; screenshots are unavailable: %s", reason)
        error = BrowserNotStarted(f"{UNAVAILABLE}: {reason}")
        error.__cause__ = cause
        return error

    async def _launch(self) -> None:
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:
            raise self._unavailable("playwright is not installed", exc) from exc
        if not redirect_refusal_supported():
            raise self._unavailable(
                "the installed websockets client cannot refuse redirects (needs websockets>=14)", None
            )
        playwright = await async_playwright().start()
        try:
            browser = await _launch_chromium(playwright, sandbox=self.sandbox)
        except Exception as exc:
            await _bounded("playwright.stop", playwright.stop())
            reason = f"Chromium did not launch: {_brief(exc)}"
            if self.sandbox:
                reason += f" (launched with {SANDBOX_SWITCH}=1; unset it to launch without the Chromium sandbox)"
            raise self._unavailable(reason, exc) from exc
        self._driver_pid = driver_pid(playwright)
        self._driver_children = driver_descendants(self._driver_pid)
        self._playwright, self._browser = playwright, browser
        self._last_used = monotonic()
        self._idle_task = asyncio.create_task(self._stop_when_idle(), name="canopy-screenshot-browser-idle")

    async def stop(self) -> None:
        """Close Chromium and the driver, each step bounded; kill the driver tree on a timeout.

        Safe to call when stopped. Always leaves the browser stopped with both
        references dropped, so the next capture launches a fresh one.
        """
        browser, playwright = self._browser, self._playwright
        pid, children = self._driver_pid, self._driver_children
        idle = self._idle_task
        self._browser = self._playwright = None
        self._driver_pid, self._driver_children = None, []
        self._idle_task = None
        if idle is not None and idle is not asyncio.current_task():
            idle.cancel()
        clean = True
        if browser is not None:
            clean = await _bounded("browser.close", browser.close())
        if clean and playwright is not None:
            clean = await _bounded("playwright.stop", playwright.stop())
        if clean:
            return
        if pid is None:
            logger.error("screenshot browser: driver pid unknown; a Chromium may linger until the census finds it")
            return
        try:
            kill_process_tree(pid, children)
        except Exception:
            logger.exception("screenshot browser: killing the driver (pid %s) failed", pid)

    async def _stop_when_idle(self) -> None:
        while True:
            await asyncio.sleep(max(0.0, self._last_used + self.idle_timeout_s - monotonic()))
            async with self.lock:
                if self._browser is None:
                    return
                if monotonic() - self._last_used < self.idle_timeout_s:
                    continue
                logger.info("screenshot browser: idle for %.0f s; stopping Chromium", self.idle_timeout_s)
                await self.stop()
                return

    @asynccontextmanager
    async def context(self) -> AsyncIterator["BrowserContext"]:
        """One capture: the lock, a launched Chromium, an origin-locked context, torn down on exit.

        Raises :class:`BrowserNotStarted` when no browser can run here.
        """
        async with self.lock:
            await self._ensure_started()
            assert self._browser is not None
            session = _Session(self.origin, self.access_token)
            self._last_session_for_tests = session
            context = await self._browser.new_context(
                viewport=self.viewport,  # type: ignore[arg-type]
                device_scale_factor=1,
                service_workers="block",
            )
            try:
                await context.route("**/*", session.route)
                await context.route_web_socket("**/*", session.route_web_socket)
                yield context
            finally:
                await session.teardown()
                try:
                    await asyncio.wait_for(context.close(), STOP_TIMEOUT_S)
                except Exception as exc:  # Chromium crashed under the capture; the next one relaunches
                    logger.warning("screenshot browser: closing the capture's context failed: %s", exc)
                self._last_used = monotonic()


# --- Process-wide browser -----------------------------------------------------------

_browser: Browser | None = None


def set_browser(browser: Browser | None) -> None:
    """Install (or, with ``None``, clear) the process-wide browser."""
    global _browser
    _browser = browser


def get_browser() -> Browser:
    """The installed process-wide browser (launched or not), or :class:`BrowserNotStarted` when there is none."""
    if _browser is None:
        raise BrowserNotStarted(f"{UNAVAILABLE}: no screenshot browser is installed")
    return _browser


def browser_hook(base_url: str, **kwargs: Any) -> Callable[[Any], AbstractAsyncContextManager[Browser]]:
    """A startup hook (see ``canopy_mcp.app.STARTUP_HOOKS``) owning the browser.

    Installs a :class:`Browser` for ``base_url`` (``kwargs`` are its keyword
    arguments: ``access_token``, ``sandbox``, ``viewport``) as
    ``app.state.browser`` and as the process-wide browser without launching
    anything, and stops it on exit under its lock.
    """

    @asynccontextmanager
    async def hook(app: Any) -> AsyncIterator[Browser]:
        browser = Browser(base_url, **kwargs)
        app.state.browser = browser
        set_browser(browser)
        try:
            yield browser
        finally:
            if _browser is browser:
                set_browser(None)
            await browser.shutdown()

    return hook
