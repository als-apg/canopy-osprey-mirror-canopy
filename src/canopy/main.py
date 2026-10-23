"""Canopy API entrypoint: composes the selected modules' sub-apps and lifespans into one FastAPI app."""

import json
import logging
from html import escape
from importlib import import_module
from contextlib import AsyncExitStack, asynccontextmanager
from collections.abc import Iterable

from fastapi import FastAPI, Request, Response, WebSocket, status
from starlette.types import Send, Scope, ASGIApp, Receive
from fastapi.responses import HTMLResponse, PlainTextResponse
from starlette.routing import Match, Mount
from starlette.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware

from canopy import __version__
from canopy.core.config import (
    MODULES_SCOPE_KEY,
    get_settings,
    parse_modules,
    resolve_modules,
)
from canopy.core.errors import (
    ErrorCode,
    KeyStoreUnavailable,
    register_canopy_error_handler,
)
from canopy.core.logging import configure_logging
from canopy.core.metrics import HTTPMetricsMiddleware, render_latest
from canopy.core.modules import tag_module, module_for_app
from canopy.core.security import (
    get_registry,
    token_from_scope,
    is_database_chain,
    load_key_registry,
)
from canopy.contracts.health import HealthDTO

configure_logging()
logger = logging.getLogger(__name__)

# Every module's name on the ``/docs`` page, in composition order (``ALL_MODULES``).
API_MODULES = (
    ("mole", "Mole"),
    ("hare", "Hare"),
    ("raccoon", "Raccoon"),
    ("vole", "Vole"),
    ("fox", "Fox"),
    ("squirrel", "Squirrel"),
    ("magpie", "Magpie"),
    ("jobs", "Jobs"),
    ("converter", "Converter"),
    ("analytics", "Analytics"),
)


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
    """Refuse every request whose key cannot read the module it targets, except ``GET /health``.

    Per-route dependencies cannot do this: the docs and OpenAPI pages of the
    parent and of each mounted sub-app, the root document and the per-module
    watchdogs have none, and a CORS preflight is answered before routing.

    The target module is the one the router would dispatch to: the first route
    that fully matches the scope, its sub-app's tag for a mount and ``core`` for
    a parent route (or none). A key is admitted iff it reads and is scoped to
    that module; each route still checks the scope it needs. Refusals: 401 for
    a missing or unknown key, 503 when the key store cannot answer, 403
    ``insufficient_module_access`` for a key scoped elsewhere and 403
    ``insufficient_scope`` for a key in the module without read.
    """

    _DETAIL = "A valid API key is required"

    def __init__(self, app: ASGIApp, *, router_app: FastAPI) -> None:
        self.app = app
        self.router_app = router_app

    def _module_for(self, scope: Scope) -> str:
        for route in self.router_app.router.routes:
            match, _ = route.matches(scope)
            if match == Match.FULL:
                return module_for_app(route.app) if isinstance(route, Mount) else "core"
        return "core"

    async def _refusal(self, scope: Scope) -> tuple[int, str, str] | None:
        """``(status, code, detail)`` of the refusal this scope earns, or ``None`` to admit it."""
        token = token_from_scope(scope)
        if token is None:
            return status.HTTP_401_UNAUTHORIZED, ErrorCode.INVALID_API_KEY.value, self._DETAIL
        try:
            key = await get_registry().get(token)
        except KeyStoreUnavailable as exc:
            return status.HTTP_503_SERVICE_UNAVAILABLE, ErrorCode.KEY_STORE_UNAVAILABLE.value, exc.detail
        if key is None:
            return status.HTTP_401_UNAUTHORIZED, ErrorCode.INVALID_API_KEY.value, self._DETAIL
        module = self._module_for(scope)
        if not key.allows_module(module):
            return (
                status.HTTP_403_FORBIDDEN,
                ErrorCode.INSUFFICIENT_MODULE_ACCESS.value,
                f"API key is not scoped to '{module}'",
            )
        if not key.read:
            return status.HTTP_403_FORBIDDEN, "insufficient_scope", "API key does not have read access"
        return None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        is_health = scope["type"] == "http" and scope["method"] == "GET" and scope["path"] == "/health"
        refused = None if is_health else await self._refusal(scope)
        if refused is None:
            await self.app(scope, receive, send)
            return

        status_code, code, detail = refused
        refusal = JSONResponse({"code": code, "detail": detail}, status_code=status_code)
        if scope["type"] == "http":
            await refusal(scope, receive, send)
            return
        websocket = WebSocket(scope, receive, send)
        if "websocket.http.response" in scope.get("extensions", {}):
            await websocket.send_denial_response(refusal)
        else:
            # A server that cannot answer a handshake with an HTTP response.
            close_code = (
                status.WS_1011_INTERNAL_ERROR
                if status_code == status.HTTP_503_SERVICE_UNAVAILABLE
                else status.WS_1008_POLICY_VIOLATION
            )
            await websocket.close(code=close_code, reason=f"{code}: {detail}")


def build_app(modules: Iterable[str] | str, *, closed_meta: bool | None = None) -> FastAPI:
    """Compose the Canopy API from the named modules.

    ``modules`` is an iterable of module names or one comma-separated string;
    an empty selection means every module and an unknown name raises
    ``ValueError``. Only the selected modules are imported, so a module whose
    dependencies are not installed costs nothing as long as it is left out.

    ``closed_meta`` puts the whole app behind the key registry except
    ``GET /health``; ``None`` takes the ``CANOPY_CLOSED_META`` setting. The key
    registry is built and bound here, once, so a keys file that cannot supply
    one raises ``KeysFileError`` before any app exists. ``/v1/api-keys`` is
    mounted only when that registry is the env + database chain.
    """
    registry = load_key_registry()
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
        # One aggregate /docs over the selected modules instead (below).
        docs_url=None,
        redoc_url=None,
    )
    app.state.modules = selected
    tag_module(app, "core")

    # Registered first so it sits INSIDE CORSMiddleware (Starlette wraps in reverse
    # registration order): preflight OPTIONS requests that CORS answers itself never
    # reach the router, and counting them would only inflate the "unmatched" series.
    app.add_middleware(HTTPMetricsMiddleware)
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
        app.add_middleware(_ClosedMeta, router_app=app)

    register_canopy_error_handler(app)

    if is_database_chain(registry):
        # Imported here: key management needs the database, which a keys file or a lean install lacks.
        from canopy.core.keys.routes import router as api_keys_router

        app.include_router(api_keys_router)

    for name, module in sub_apps.items():
        tag_module(module.app, name)
        app.mount(f"/{name}", module.app)

    @app.get("/health", tags=["meta"])
    async def health() -> HealthDTO:
        return HealthDTO.of()

    @app.get("/metrics", tags=["meta"], response_class=PlainTextResponse)
    async def metrics() -> Response:
        """Prometheus exposition for this process."""
        payload, content_type = render_latest()
        return Response(content=payload, media_type=content_type)

    docs_specs = [(name, title) for name, title in API_MODULES if name in selected]

    @app.get("/docs", include_in_schema=False, response_class=HTMLResponse)
    async def docs_ui(request: Request) -> HTMLResponse:
        root_path = request.scope.get("root_path", "").rstrip("/")
        specs = [
            {"url": f"{root_path}/openapi.json", "name": "Canopy"},
            *[{"url": f"{root_path}/{path}/openapi.json", "name": name} for path, name in docs_specs],
        ]
        serialized_specs = json.dumps(specs).replace("<", "\\u003c")
        return HTMLResponse(
            f"""<!DOCTYPE html>
<html>
<head>
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<link type="text/css" rel="stylesheet" href="https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui.css">
<title>{escape(f"{app.title} - Swagger UI")}</title>
</head>
<body>
<div id="swagger-ui"></div>
<script src="https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui-bundle.js"></script>
<script src="https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui-standalone-preset.js"></script>
<script>
const ui = SwaggerUIBundle({{
    dom_id: "#swagger-ui",
    urls: {serialized_specs},
    "urls.primaryName": "Canopy",
    validatorUrl: null,
    presets: [
        SwaggerUIBundle.presets.apis,
        SwaggerUIStandalonePreset
    ],
    layout: "StandaloneLayout",
}});
</script>
</body>
</html>"""
        )

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
