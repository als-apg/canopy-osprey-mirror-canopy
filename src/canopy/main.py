import logging
from importlib import import_module
from contextlib import AsyncExitStack, asynccontextmanager
from collections.abc import Iterable

from fastapi import FastAPI, WebSocket, status
from starlette.types import Send, Scope, ASGIApp, Receive
from starlette.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware

from canopy import __version__
from canopy.core.config import (
    MODULES_SCOPE_KEY,
    get_settings,
    parse_modules,
    resolve_modules,
)
from canopy.core.errors import ErrorCode, register_canopy_error_handler
from canopy.core.logging import configure_logging
from canopy.core.security import key_from_scope, load_key_registry

configure_logging()
logger = logging.getLogger(__name__)


class _SelectedModules:
    """Publish the app's module selection on every ASGI scope.

    Mounted sub-apps are shared singletons and cannot see the app they are
    mounted in; the scope is what reaches them, so a route can tell whether a
    peer module is part of this deployment (``canopy.core.config.module_selected``).
    """

    def __init__(self, app: ASGIApp, modules: frozenset[str]) -> None:
        self.app = app
        self.modules = modules

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        scope[MODULES_SCOPE_KEY] = self.modules
        await self.app(scope, receive, send)


class _ClosedMeta:
    """Refuse every request that carries no registry key, except ``GET /health``.

    Per-route dependencies cannot do this: the docs and OpenAPI pages of the
    parent and of each mounted sub-app, the root document and the per-module
    watchdogs have none, and a CORS preflight is answered before routing. The
    gate only asks for *a* key; each route still checks the scope it needs.
    """

    _DETAIL = "A valid API key is required"

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        is_health = scope["type"] == "http" and scope["method"] == "GET" and scope["path"] == "/health"
        key = None if is_health else key_from_scope(scope)
        # A registry entry that grants no scope opens nothing.
        if is_health or (key is not None and (key.read or key.write)):
            await self.app(scope, receive, send)
            return

        refusal = JSONResponse(
            {"code": ErrorCode.INVALID_API_KEY.value, "detail": self._DETAIL},
            status_code=status.HTTP_401_UNAUTHORIZED,
        )
        if scope["type"] == "http":
            await refusal(scope, receive, send)
            return
        websocket = WebSocket(scope, receive, send)
        if "websocket.http.response" in scope.get("extensions", {}):
            await websocket.send_denial_response(refusal)
        else:
            # A server that cannot answer a handshake with an HTTP response.
            await websocket.close(
                code=status.WS_1008_POLICY_VIOLATION,
                reason=f"{ErrorCode.INVALID_API_KEY.value}: {self._DETAIL}",
            )


def build_app(modules: Iterable[str] | str, *, closed_meta: bool | None = None) -> FastAPI:
    """Compose the Canopy API from the named modules.

    ``modules`` is an iterable of module names or one comma-separated string;
    an empty selection means every module and an unknown name raises
    ``ValueError``. Only the selected modules are imported, so a module whose
    dependencies are not installed costs nothing as long as it is left out.

    ``closed_meta`` puts the whole app behind the key registry except
    ``GET /health``; ``None`` takes the ``CANOPY_CLOSED_META`` setting. The key
    registry is loaded here, so a keys file that cannot supply one raises
    ``KeysFileError`` before any app exists.
    """
    load_key_registry()
    if closed_meta is None:
        closed_meta = get_settings().closed_meta

    selected = parse_modules(modules) if isinstance(modules, str) else resolve_modules(modules)

    # Import here, not at module top: an unselected sub-app must never load.
    sub_apps = {name: import_module(f"canopy.{name}.app") for name in selected}

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        """Compose per-module lifespans. Mounted sub-apps don't run their own."""
        async with AsyncExitStack() as stack:
            for name, module in sub_apps.items():
                await stack.enter_async_context(getattr(module, f"{name}_lifespan")())
            logger.info(f"Starting Canopy API [version {__version__}] with modules: {', '.join(selected)}")
            yield
        logger.info("Canopy API shutdown complete")

    app = FastAPI(
        title="Canopy",
        description="Control-system display platform — backend API",
        version=__version__,
        lifespan=lifespan,
    )
    app.state.modules = selected

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.add_middleware(_SelectedModules, modules=frozenset(selected))
    if closed_meta:
        # Added last, so it is the outermost layer: a keyless preflight is
        # refused here and never reaches CORS.
        app.add_middleware(_ClosedMeta)

    register_canopy_error_handler(app)

    for name, module in sub_apps.items():
        app.mount(f"/{name}", module.app)

    @app.get("/health", tags=["meta"])
    async def health() -> dict[str, str]:
        return {"status": "healthy"}

    @app.get("/", tags=["meta"])
    async def root() -> dict[str, str]:
        return {
            "name": "Canopy",
            "version": __version__,
            "docs": "/docs",
            "health": "/health",
        }

    return app


app = build_app(get_settings().modules)
