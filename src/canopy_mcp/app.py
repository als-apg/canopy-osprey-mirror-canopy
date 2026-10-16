"""The ASGI app: the MCP endpoint and the viewer on one port.

``/mcp`` is FastMCP's streamable-HTTP route, lifted onto this app (rather than
mounted) so ``POST /mcp`` answers directly instead of redirecting to
``/mcp/``. The viewer and draft routes come from :mod:`canopy_mcp.static`, the
saved-view routes (``/views``) from :mod:`canopy_mcp.views`; the
API forward (two WebSocket relays, the Mole GET, with ``CANOPY_PUT_RELAY`` on
the one PV write ``PUT /hare/v1/pvs/{pv}``, 405 elsewhere) from
:mod:`canopy_mcp.forward`.

The MCP endpoint is for a local agent only. A request carrying
``X-Forwarded-Prefix`` or ``X-Osprey-Terminal-Secret`` came through the OSPREY
proxy -- i.e. from a browser session -- so :class:`McpProxyGuard` refuses it
with 403 before it reaches FastMCP. The viewer routes stay reachable through
the proxy; that is what they are for.

A deployment that sets ``CANOPY_ACCESS_TOKEN_FILE`` gets
:class:`~canopy_mcp.access.AccessGate` in the guard's place: every route,
``/mcp`` included, then answers only a request carrying the credential, and
the proxy that adds it is how ``/mcp`` is reached. ``GET /health`` stays open.
Either way ``/mcp/`` is a redirect to ``/mcp``, never the transport itself.
The PUT relay is only ever served behind the gate: an app asked for the relay
without an access-token file is refused (:class:`~canopy_mcp.config.RelayConfigError`).

One lifespan first flattens the color scheme (:func:`~canopy.core.color_scheme.get_color_scheme`,
so a bad ``CANOPY_THEME_FILE`` logs its one ERROR at boot, not on the first
request), then runs FastMCP's session manager, opens the shared backend
``httpx.AsyncClient`` (``app.state.http``) and then enters every
:data:`STARTUP_HOOKS` entry in order; hooks are exited in reverse on shutdown.
The defaults load the widget catalog (:func:`catalog_hook`) and install the
lazy screenshot browser (:func:`chromium_hook`, which launches nothing until
the first ``screenshot`` call); neither keeps the server from starting when
it fails. Building the app calls
:func:`~canopy_mcp.server.register_all`, so every tool is on ``mcp``, and then
applies ``CANOPY_AUTHORING``: off takes the seven authoring tools off ``mcp``,
leaving the ten that only read. ``mcp`` is process-wide, so the last app
built decides which tools every app in the process offers.
"""

import logging
from typing import Any
from contextlib import AsyncExitStack, AbstractAsyncContextManager, asynccontextmanager
from collections.abc import Callable, AsyncIterator

from starlette.types import Send, Scope, ASGIApp, Receive
from starlette.responses import PlainTextResponse
from starlette.middleware import Middleware
from starlette.applications import Starlette

from canopy_mcp.store import open_stores
from canopy_mcp.views import ViewStore, build_view_routes
from canopy_mcp.access import AccessGate, read_secret_file
from canopy_mcp.client import make_client
from canopy_mcp.config import Settings, RelayConfigError, get_settings
from canopy_mcp.server import mcp, register_all, set_authoring
from canopy_mcp.static import build_routes
from canopy_mcp.browser import browser_hook
from canopy_mcp.catalog import CatalogError, set_catalog, load_catalog
from canopy_mcp.forward import build_forward_routes
from canopy_mcp.__main__ import DEFAULT_HOST, DEFAULT_PORT
from canopy_mcp.collections import (
    CollectionStore,
    FacilityCollections,
    build_collection_routes,
)
from canopy.core.color_scheme import get_color_scheme

MCP_PATH = "/mcp"
PROXY_HEADERS = (b"x-forwarded-prefix", b"x-osprey-terminal-secret")

DEFAULT_SELF_URL = f"http://{DEFAULT_HOST}:{DEFAULT_PORT}"

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
    credential on every request it makes there. On shutdown a running Chromium
    is stopped under the browser's lock. A host without a browser starts anyway.
    """
    settings: Settings = app.state.settings
    token = None
    if settings.access_token_file is not None:
        token = read_secret_file("CANOPY_ACCESS_TOKEN_FILE", settings.access_token_file)
    return browser_hook(app.state.self_url, access_token=token, sandbox=settings.browser_sandbox)(app)


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
    self_url: str | None = None,
) -> Starlette:
    """Build the app; ``startup_hooks`` defaults to the module's :data:`STARTUP_HOOKS`.

    ``self_url`` is the address this server listens on, which the screenshot
    browser loads the viewer from; left out, it is ``CANOPY_SELF_URL`` and then
    :data:`DEFAULT_SELF_URL`.

    A configured access-token file is read before anything else is built, so
    one that cannot supply a credential raises here and nothing is served,
    and so does a PUT relay without one.
    """
    settings = settings or get_settings()
    if settings.put_relay and (settings.access_token_file is None or not settings.write_api_key):
        raise RelayConfigError("the PUT relay needs CANOPY_ACCESS_TOKEN_FILE and CANOPY_WRITE_API_KEY_FILE")
    if settings.access_token_file is not None:
        token = read_secret_file("CANOPY_ACCESS_TOKEN_FILE", settings.access_token_file)
        guard = Middleware(AccessGate, token=token)
    else:
        guard = Middleware(McpProxyGuard)
    register_all()
    set_authoring(settings.authoring)
    store, library = open_stores(settings.drafts_dir, settings.library_dir)
    views = ViewStore(settings.drafts_dir, views_dir=settings.views_dir)
    facility = (
        FacilityCollections(settings.facility_collections_dir)
        if settings.facility_collections_dir is not None
        else None
    )
    collections = CollectionStore(settings.personal_collections_dir, facility=facility)
    mcp_app = mcp.http_app(path=MCP_PATH)

    @asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        hooks = STARTUP_HOOKS if startup_hooks is None else startup_hooks
        get_color_scheme()  # never raises; a bad file degrades /health/summary and serves canopy-default
        async with mcp_app.lifespan(app), make_client(settings) as client, AsyncExitStack() as stack:
            app.state.http = client
            for hook in list(hooks):
                await stack.enter_async_context(hook(app))
            yield

    app = Starlette(
        routes=[
            *mcp_app.routes,
            *build_forward_routes(settings),
            *build_routes(settings, store, library),
            *build_view_routes(views),
            *build_collection_routes(collections, facility),
        ],
        # FastMCP's own middleware (request context, host/origin guard) must
        # wrap its routes here too, since only the routes were lifted.
        middleware=[guard, *mcp_app.user_middleware],
        lifespan=lifespan,
    )
    for key in ("fastmcp_server", "path", "transport_type"):
        setattr(app.state, key, getattr(mcp_app.state, key))
    app.state.settings = settings
    app.state.store = store
    app.state.library = library
    app.state.views = views
    app.state.collections = collections
    app.state.facility_collections = facility
    app.state.self_url = (self_url or settings.self_url or DEFAULT_SELF_URL).rstrip("/")
    return app


app = create_app()
