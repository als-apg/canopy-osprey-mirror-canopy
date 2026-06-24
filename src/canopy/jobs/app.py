import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from canopy.jobs import gateway
from canopy.jobs.runtime import mark_started
from canopy.core.config import get_settings
from canopy.core.errors import register_canopy_error_handler
from canopy.jobs.api.v1.router import router as v1_router

logger = logging.getLogger(__name__)


@asynccontextmanager
async def jobs_lifespan():
    """Record the Redis URL (pool connects lazily) and expose job status.

    Composed by the parent app's lifespan in `canopy.main`; mounted sub-apps
    don't run their own lifespan behind `app.mount()`.
    """
    gateway.configure(get_settings().redis_url)
    mark_started()
    logger.info("Jobs started: Arq pool will connect on first use")
    try:
        yield
    finally:
        await gateway.close_pool()
        logger.info("Jobs shutdown complete")


app = FastAPI(
    title="Canopy / Jobs",
    description="Background-job status over the Arq queue.",
)

register_canopy_error_handler(app)
app.include_router(v1_router)
