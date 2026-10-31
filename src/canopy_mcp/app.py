"""The ASGI app: the MCP endpoint and the viewer on one port.

``/mcp`` is FastMCP's streamable-HTTP route, lifted onto this app (rather than
mounted) so ``POST /mcp`` answers directly instead of redirecting to
``/mcp/``. The viewer and draft routes come from :mod:`canopy_mcp.static` and
the API forward (two WebSocket relays, the Mole GET, the Fox POSTs and, with
``CANOPY_PUT_RELAY`` on, the one PV write) from :mod:`canopy_mcp.forward`.

Who may reach ``/mcp`` is decided by one of two guards. Without an access token,
:class:`ProxiedMcpGuard` refuses ``/mcp`` to any request that came through the
embedding host's proxy, i.e. from a browser session; every other route stays
reachable through the proxy, which is what the viewer routes are for. With
``CANOPY_ACCESS_TOKEN_FILE`` set, :class:`~canopy_mcp.access.AccessGate` takes
its place: every route, ``/mcp`` included, answers only a request carrying the
credential, and the proxy that adds it is how ``/mcp`` is reached. ``GET /health``
stays open either way, and ``/mcp/`` is a redirect to ``/mcp``, never the
transport itself.

One lifespan first flattens the color scheme (:func:`~canopy.core.color_scheme.get_color_scheme`,
so a bad ``CANOPY_THEME_FILE`` logs its one ERROR at boot, not on the first
request), then runs FastMCP's session manager, opens the shared backend
``httpx.AsyncClient`` (``app.state.http``) and then enters every
:data:`STARTUP_HOOKS` entry in order; they are exited
in reverse on shutdown. The defaults load the widget catalog (:func:`catalog_hook`)
and install the lazy screenshot browser (:func:`chromium_hook`, which launches
nothing until the first ``screenshot`` call); neither keeps the server from
starting when it fails.

Building the app calls :func:`~canopy_mcp.server.register_all`, so every tool is
on ``mcp``, and then applies ``CANOPY_AUTHORING`` (:func:`~canopy_mcp.server.set_authoring`).
``mcp`` is process-wide, so the last app built decides which tools every app in
the process offers. Nothing is built at import: ``python -m canopy_mcp`` calls
:func:`create_app`.
"""

import logging
from typing import Any
from contextlib import AsyncExitStack, AbstractAsyncContextManager, asynccontextmanager
from collections.abc import Callable, Iterable, AsyncIterator

from starlette.types import Send, Scope, ASGIApp, Receive
from starlette.responses import PlainTextResponse
from starlette.middleware import Middleware
from starlette.applications import Starlette

from canopy_mcp.store import open_stores
from canopy_mcp.access import AccessGate, read_secret_file
from canopy_mcp.client import make_client
from canopy_mcp.config import DEFAULT_SELF_URL, Settings, get_settings
from canopy_mcp.server import mcp, register_all, set_authoring
from canopy_mcp.static import build_routes
from canopy_mcp.browser import browser_hook
from canopy_mcp.catalog import CatalogError, set_catalog, load_catalog
from canopy_mcp.forward import build_forward_routes
from canopy.core.color_scheme import get_color_scheme

MCP_PATH = "/mcp"

StartupHook = Callable[[Starlette], AbstractAsyncContextManager[Any]]

logger = logging.getLogger(__name__)


@asynccontextmanager
async def catalog_hook(app: Starlette) -> AsyncIterator[None]:
    """Load the widget catalog once at startup.

    A viewer dist without ``ir-map.json`` or an unreadable staged spec must not
    keep the server (and its viewer) from starting: the failure is logged and
    ``ensure_catalog`` retries on the first tool call that needs it.
    """
    try:
        set_catalog(await load_catalog(app.state.settings))
    except CatalogError as exc:
        logger.warning("widget catalog not loaded at startup (retried on first use): %s", exc)
    yield


def chromium_hook(app: Starlette) -> AbstractAsyncContextManager[Any]:
    """Install the lazy screenshot browser; nothing launches until the first ``screenshot`` call.

    It loads this server's own viewer routes, so it is pointed at
    ``app.state.self_url`` and, on a gated server, carries the access
    credential (``app.state.access_token``) on every request it makes there. On
    shutdown a running Chromium is stopped under the browser's lock. A host
    without a browser starts anyway.
    """
    settings: Settings = app.state.settings
    return browser_hook(app.state.self_url, access_token=app.state.access_token, sandbox=settings.browser_sandbox)(app)


# Entered in order at startup, after the shared client exists.
STARTUP_HOOKS: list[StartupHook] = [catalog_hook, chromium_hook]


class ProxiedMcpGuard:
    """403 for a request to ``/mcp`` that came through the embedding host's proxy.

    A request carrying any of ``mark_headers`` (``X-Forwarded-Prefix``, plus the
    names in ``CANOPY_PROXY_MARK_HEADERS``) is taken to have come through the
    proxy, from a browser session. A direct caller simply sends none of them, so
    this keeps a browser off the agent's endpoint but is no authentication: it
    is only safe while the server listens on loopback. A configured access
    token replaces this guard with :class:`~canopy_mcp.access.AccessGate`.
    """

    def __init__(self, app: ASGIApp, mark_headers: Iterable[str] = ("x-forwarded-prefix",)):
        self.app = app
        self.mark_headers = frozenset(name.lower().encode("latin-1") for name in mark_headers)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope["path"].rstrip("/") == MCP_PATH:
            names = {name.lower() for name, _ in scope.get("headers", ())}
            if not self.mark_headers.isdisjoint(names):
                response = PlainTextResponse("the MCP endpoint is not reachable through the proxy", status_code=403)
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


def create_app(
    settings: Settings | None = None,
    *,
    startup_hooks: list[StartupHook] | None = None,
    self_url: str | None = None,
) -> Starlette:
    """Build the app; ``startup_hooks`` defaults to the module's :data:`STARTUP_HOOKS`.

    ``self_url`` is the address this server listens on, which the screenshot
    browser loads the viewer from; left out, it is ``CANOPY_SELF_URL`` and then
    :data:`~canopy_mcp.config.DEFAULT_SELF_URL`.

    A configured access-token file is read once, before anything else is built,
    so one that cannot supply a credential raises here and nothing is served.
    """
    settings = settings or get_settings()
    token = None
    if settings.access_token_file is not None:
        token = read_secret_file("CANOPY_ACCESS_TOKEN_FILE", settings.access_token_file)
        guard = Middleware(AccessGate, token=token)
    else:
        guard = Middleware(ProxiedMcpGuard, mark_headers=settings.proxy_mark_header_names)
    register_all()
    set_authoring(settings.authoring)
    store, library = open_stores(settings.drafts_dir, settings.library_dir)
    mcp_app = mcp.http_app(path=MCP_PATH)

    @asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        hooks = STARTUP_HOOKS if startup_hooks is None else startup_hooks
        get_color_scheme()  # never raises; a bad file degrades /health/summary and serves canopy-default
        async with mcp_app.lifespan(app), make_client(settings) as client, AsyncExitStack() as stack:
            app.state.http = client
            for hook in hooks:
                await stack.enter_async_context(hook(app))
            yield

    app = Starlette(
        routes=[
            *mcp_app.routes,
            *build_forward_routes(settings),
            *build_routes(settings, store, library),
        ],
        # FastMCP's own middleware (request context, host/origin guard) must
        # wrap its routes here too, since only the routes were lifted.
        middleware=[guard, *mcp_app.user_middleware],
        lifespan=lifespan,
    )
    for key in ("fastmcp_server", "path", "transport_type"):
        setattr(app.state, key, getattr(mcp_app.state, key))
    app.state.settings = settings
    app.state.access_token = token
    app.state.store = store
    app.state.library = library
    app.state.self_url = (self_url or settings.self_url or DEFAULT_SELF_URL).rstrip("/")
    return app
