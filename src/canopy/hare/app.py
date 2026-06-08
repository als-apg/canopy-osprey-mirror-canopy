import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from canopy.hare import gateway
from canopy.core.errors import register_canopy_error_handler
from canopy.hare.config import get_hare_config
from canopy.epics.config import get_epics_config
from canopy.hare.runtime import mark_started
from canopy.epics.gateway import build_connector
from canopy.hare.api.v1.router import router as v1_router
from canopy.hare.services.broker import PVBroker

logger = logging.getLogger(__name__)


@asynccontextmanager
async def hare_lifespan():
    """Startup: build the connector + broker, start it, bind to the gateway.

    Composed by the parent app's lifespan in ``canopy.main``. Mounted sub-apps
    don't run their own lifespan automatically behind ``app.mount()``.
    """
    epics_config = get_epics_config()
    hare_config = get_hare_config()
    connector = build_connector(epics_config)
    broker = PVBroker(connector, hare_config)
    await broker.start()
    gateway.bind(broker)
    mark_started()
    logger.info("Hare started: EPICS backend=%r", epics_config.backend)
    try:
        yield
    finally:
        await broker.stop()
        gateway.bind(None)
        logger.info("Hare shutdown complete")


app = FastAPI(
    title="Canopy / Hare",
    description="Live-PV WebSocket broker.",
)

register_canopy_error_handler(app)
app.include_router(v1_router)
