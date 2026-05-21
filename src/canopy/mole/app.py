import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from canopy.mole import gateway
from canopy.core.errors import register_canopy_error_handler
from canopy.mole.config import get_mole_config
from canopy.mole.runtime import mark_started
from canopy.mole.api.v1.router import router as v1_router
from canopy.mole.adapters.registry import build_registry
from canopy.mole.services.archiver_service import ArchiverService

logger = logging.getLogger(__name__)


@asynccontextmanager
async def mole_lifespan():
    """Startup: build the registry, construct the service, bind to the gateway.

    Composed by the parent app's lifespan in `canopy.main` — mounted sub-apps
    don't run their own lifespan automatically behind `app.mount()`.
    """
    config = get_mole_config()
    registry = build_registry(config)
    service = ArchiverService(registry, config)
    gateway.bind(service)
    mark_started()
    logger.info(
        "Mole started: %d archivers configured, default=%r",
        len(registry.names()),
        registry.default_name or None,
    )
    try:
        yield
    finally:
        await registry.close_all()
        gateway.bind(None)
        logger.info("Mole shutdown complete")


app = FastAPI(
    title="Canopy / Mole",
    description="Archiver-appliance proxy.",
)

register_canopy_error_handler(app)
app.include_router(v1_router)
