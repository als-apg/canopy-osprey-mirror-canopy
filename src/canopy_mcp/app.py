"""The ASGI app: the MCP endpoint and the viewer on one port.

``/mcp`` is FastMCP's streamable-HTTP route, lifted onto this app (rather than
mounted) so ``POST /mcp`` answers directly instead of redirecting to
``/mcp/``. The viewer and draft routes come from :mod:`canopy_mcp.static`, the
saved-view routes (``/views``) from :mod:`canopy_mcp.views`; the
read-only API forward (two WebSocket relays, 405 elsewhere) from
:mod:`canopy_mcp.forward`.

The MCP endpoint is for a local agent only. A request carrying
``X-Forwarded-Prefix`` or ``X-Osprey-Terminal-Secret`` came through the OSPREY
proxy -- i.e. from a browser session -- so :class:`McpProxyGuard` refuses it
with 403 before it reaches FastMCP. The viewer routes stay reachable through
the proxy; that is what they are for.

One lifespan runs FastMCP's session manager, opens the shared backend
``httpx.AsyncClient`` (``app.state.http``) and then enters every
:data:`STARTUP_HOOKS` entry in order; hooks are exited in reverse on shutdown.
The defaults load the widget catalog (:func:`catalog_hook`) and launch the
screenshot Chromium (:func:`chromium_hook`). Building the app calls
:func:`~canopy_mcp.server.register_all`, so every tool is on ``mcp``.
"""

import logging

from typing import Any
from contextlib import AsyncExitStack, AbstractAsyncContextManager, asynccontextmanager
from collections.abc import Callable, AsyncIterator

import httpx
from starlette.types import Send, Scope, ASGIApp, Receive
from starlette.responses import PlainTextResponse
from starlette.middleware import Middleware
from starlette.applications import Starlette

from canopy_mcp.store import DraftStore
from canopy_mcp.browser import browser_hook
from canopy_mcp.catalog import CatalogError, set_catalog, load_catalog
from canopy_mcp.client import make_client
from canopy_mcp.config import Settings, get_settings
from canopy_mcp.server import mcp, register_all
from canopy_mcp.__main__ import DEFAULT_HOST, DEFAULT_PORT
from canopy_mcp.views import ViewStore, build_view_routes
from canopy_mcp.static import build_routes
from canopy_mcp.forward import build_forward_routes

MCP_PATH = "/mcp"
PROXY_HEADERS = (b"x-forwarded-prefix", b"x-osprey-terminal-secret")

DEFAULT_SELF_URL = f"http://{DEFAULT_HOST}:{DEFAULT_PORT}"

StartupHook = Callable[[Starlette], AbstractAsyncContextManager[Any]]

logger = logging.getLogger(__name__)


@asynccontextmanager
async def catalog_hook(app: Starlette) -> AsyncIterator[None]:
    """Load the widget catalog once at startup, over the shared client.

    A backend that is not up yet, or a viewer dist without ``ir-map.json``,
    must not keep the server (and its viewer) from starting: the failure is
    logged and ``ensure_catalog`` retries on the first tool call that needs it.
    """
    try:
        set_catalog(await load_catalog(app.state.settings, app.state.http))
    except (CatalogError, httpx.HTTPError) as exc:
        logger.warning("widget catalog not loaded at startup (retried on first use): %s", exc)
    yield


def chromium_hook(app: Starlette) -> AbstractAsyncContextManager[Any]:
    """Launch the screenshot Chromium at startup and close it on shutdown.

    It loads this server's own viewer routes, so it is pointed at
    ``app.state.self_url``.
    """
    return browser_hook(app.state.self_url)(app)


# Entered in order at startup, after the shared client exists.
STARTUP_HOOKS: list[StartupHook] = [catalog_hook, chromium_hook]


class McpProxyGuard:
    """403 for requests to ``/mcp`` that arrived through the OSPREY proxy."""

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope["path"].rstrip("/") == MCP_PATH:
            names = {name.lower() for name, _ in scope.get("headers", ())}
            if any(header in names for header in PROXY_HEADERS):
                response = PlainTextResponse("the MCP endpoint is not reachable through the proxy", status_code=403)
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


def create_app(
    settings: Settings | None = None,
    *,
    startup_hooks: list[StartupHook] | None = None,
    self_url: str = DEFAULT_SELF_URL,
) -> Starlette:
    """Build the app; ``startup_hooks`` defaults to the module's :data:`STARTUP_HOOKS`.

    ``self_url`` is the address this server listens on, which the screenshot
    browser loads the viewer from.
    """
    settings = settings or get_settings()
    register_all()
    store = DraftStore(settings.drafts_dir)
    views = ViewStore(settings.drafts_dir)
    mcp_app = mcp.http_app(path=MCP_PATH)

    @asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        hooks = STARTUP_HOOKS if startup_hooks is None else startup_hooks
        async with mcp_app.lifespan(app), make_client(settings) as client, AsyncExitStack() as stack:
            app.state.http = client
            for hook in list(hooks):
                await stack.enter_async_context(hook(app))
            yield

    app = Starlette(
        routes=[
            *mcp_app.routes,
            *build_forward_routes(settings),
            *build_routes(settings, store),
            *build_view_routes(views),
        ],
        # FastMCP's own middleware (request context, host/origin guard) must
        # wrap its routes here too, since only the routes were lifted.
        middleware=[Middleware(McpProxyGuard), *mcp_app.user_middleware],
        lifespan=lifespan,
    )
    for key in ("fastmcp_server", "path", "transport_type"):
        setattr(app.state, key, getattr(mcp_app.state, key))
    app.state.settings = settings
    app.state.store = store
    app.state.views = views
    app.state.self_url = self_url.rstrip("/")
    return app


app = create_app()
