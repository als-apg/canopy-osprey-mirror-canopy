import logging
from importlib import import_module
from contextlib import AsyncExitStack, asynccontextmanager
from collections.abc import Iterable

from fastapi import FastAPI
from starlette.types import Send, Scope, ASGIApp, Receive
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


def build_app(modules: Iterable[str] | str) -> FastAPI:
    """Compose the Canopy API from the named modules.

    ``modules`` is an iterable of module names or one comma-separated string;
    an empty selection means every module and an unknown name raises
    ``ValueError``. Only the selected modules are imported, so a module whose
    dependencies are not installed costs nothing as long as it is left out.
    """
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
