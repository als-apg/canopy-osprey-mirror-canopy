import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from canopy.beaver import gateway
from canopy.core.errors import register_canopy_error_handler
from canopy.beaver.config import get_beaver_config
from canopy.beaver.runtime import mark_started
from canopy.beaver.api.v1.router import router as v1_router
from canopy.beaver.catalog.loader import FileCatalogLoader
from canopy.beaver.services.widget_service import WidgetService

logger = logging.getLogger(__name__)


@asynccontextmanager
async def beaver_lifespan():
    """Load the widget catalog, validate against meta-schema, bind the service.

    Composed by the parent app's lifespan in ``canopy.main``. Mounted sub-apps
    don't run their own lifespan automatically behind ``app.mount()``.
    """
    config = get_beaver_config()
    loader = FileCatalogLoader(config)
    index = loader.load()
    service = WidgetService(index, config)
    gateway.bind(service)
    mark_started()
    logger.info(
        "Beaver started: %d widgets, %d categories, registry %s",
        index.widget_count,
        index.category_count,
        index.registry_version,
    )
    try:
        yield
    finally:
        gateway.bind(None)
        logger.info("Beaver shutdown complete")


app = FastAPI(
    title="Canopy / Beaver",
    description="Widget registry — metadata + JSON Schemas for Canopy widgets.",
)

register_canopy_error_handler(app)
app.include_router(v1_router)
