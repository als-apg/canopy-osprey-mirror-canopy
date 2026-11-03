"""Canopy API entrypoint: composes the selected modules' sub-apps and lifespans into one FastAPI app."""

import json
import logging
from html import escape
from importlib import import_module
from contextlib import AsyncExitStack, asynccontextmanager
from collections.abc import Iterable

from fastapi import FastAPI, Request, Response
from starlette.types import Send, Scope, ASGIApp, Receive
from fastapi.responses import HTMLResponse, PlainTextResponse
from fastapi.middleware.cors import CORSMiddleware

from canopy import __version__
from canopy.core.config import (
    MODULES_SCOPE_KEY,
    get_settings,
    parse_modules,
    resolve_modules,
)
from canopy.core.errors import register_canopy_error_handler
from canopy.core.logging import configure_logging
from canopy.core.metrics import HTTPMetricsMiddleware, render_latest
from canopy.core.modules import tag_module
from canopy.core.security import (
    bind_key_registry,
    has_database_tier,
    build_key_registry,
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
    ("formula-engine", "Formula Engine"),
    ("squirrel", "Squirrel"),
    ("magpie", "Magpie"),
    ("jobs", "Jobs"),
    ("converter", "Converter"),
    ("analytics", "Analytics"),
    ("elog", "E-log"),
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


async def _facility_granted_to_key(facility_id: str, slug: str) -> bool:
    """Is any active API key's facility grant still naming this facility?

    Registered from here, not from ``core.keys``, to keep ``core.facilities`` free of
    imports out of ``core.keys`` — the reverse edge already exists, since
    ``keys.service`` validates grants through the facilities gateway. Grants store
    slugs, so ``facility_id`` is unused. Imported here: it needs the database tier.
    """
    from canopy.core.db import session_scope
    from canopy.core.keys.repositories import ApiKeyRepository

    async with session_scope() as session:
        return await ApiKeyRepository(session).count_granting(slug) > 0


def build_app(modules: Iterable[str] | str) -> FastAPI:
    """Compose the Canopy API from the named modules.

    ``modules`` is an iterable of module names or one comma-separated string;
    an empty selection means every module and an unknown name raises
    ``ValueError``. Only the selected modules are imported, so a module whose
    dependencies are not installed costs nothing as long as it is left out.

    The key registry is bound by the lifespan, before anything is served.
    ``/v1/api-keys`` and ``/v1/facilities`` are mounted only on an install with
    the database key tier (``has_database_tier``).
    """
    database_tier = has_database_tier()

    selected = parse_modules(modules) if isinstance(modules, str) else resolve_modules(modules)

    # Import here, not at module top: an unselected sub-app must never load. A
    # module's label is its mount and key scope; its package spells "-" as "_".
    packages = {name: name.replace("-", "_") for name in selected}
    sub_apps = {name: import_module(f"canopy.{package}.app") for name, package in packages.items()}

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        """Compose per-module lifespans. Mounted sub-apps don't run their own."""
        # Env keys first, then the database when the install has it: the
        # long-lived credentials keep working when Postgres is unreachable,
        # including on a cold start when the cache has nothing stale to serve.
        # Built before anything can serve, so a bad CANOPY_API_KEYS surfaces in
        # the startup log instead of as an unexplained 401.
        bind_key_registry(build_key_registry(get_settings()))
        if database_tier:
            from canopy.core.facilities import gateway as facilities_gateway

            facilities_gateway.register_in_use_checker(_facility_granted_to_key, "API key grants")
        async with AsyncExitStack() as stack:
            for name, module in sub_apps.items():
                await stack.enter_async_context(getattr(module, f"{packages[name]}_lifespan")())
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

    register_canopy_error_handler(app)

    if database_tier:
        # Imported here: key and facility management need the database, which a lean install lacks.
        from canopy.core.keys.routes import router as api_keys_router
        from canopy.core.facilities.routes import router as facilities_router

        app.include_router(api_keys_router)
        app.include_router(facilities_router)

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
