import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from canopy.core.errors import register_canopy_error_handler
from canopy.squirrel.runtime import mark_started
from canopy.squirrel.api.v1.router import router as v1_router

logger = logging.getLogger(__name__)


@asynccontextmanager
async def squirrel_lifespan():
    """Record boot time and nothing more.

    Squirrel's services are request-scoped (built per request from a DB session) and the
    shared engine + Jobs Redis pool both connect lazily, so there is no singleton to wire
    here. Composed by the parent app's lifespan in ``canopy.main``.
    Mounted sub-apps don't run their own behind ``app.mount()``.
    """
    mark_started()
    logger.info("Squirrel started")
    try:
        yield
    finally:
        logger.info("Squirrel shutdown complete")


app = FastAPI(
    title="Canopy / Squirrel",
    description="EPICS machine-state snapshots: save, restore, and compare.",
)

register_canopy_error_handler(app)
app.include_router(v1_router)
