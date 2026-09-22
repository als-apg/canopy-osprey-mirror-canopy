import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from canopy.analytics import gateway
from canopy.core.errors import register_canopy_error_handler
from canopy.analytics.config import get_analytics_config
from canopy.analytics.runtime import mark_started
from canopy.analytics.api.v1.router import router as v1_router
from canopy.analytics.services.analytics_service import AnalyticsService

logger = logging.getLogger(__name__)


@asynccontextmanager
async def analytics_lifespan():
    """Composed by the parent app's lifespan in ``canopy.main``.

    Mounted sub-apps don't run their own lifespan behind ``app.mount()``.
    Startup reads the config once and binds the AnalyticsService to the
    gateway, so the state logged here is the one that serves traffic.
    """
    config = get_analytics_config()
    gateway.bind(AnalyticsService(config))
    mark_started()
    logger.info(
        "Analytics started: ingestion %s, max_event_bytes=%d",
        "enabled" if config.enabled else "DISABLED",
        config.max_event_bytes,
    )
    try:
        yield
    finally:
        gateway.bind(None)
        logger.info("Analytics shutdown complete")


app = FastAPI(
    title="Canopy / Analytics",
    description="Frontend usage + error event ingestion, recorded as structured log lines.",
)

register_canopy_error_handler(app)
app.include_router(v1_router)
