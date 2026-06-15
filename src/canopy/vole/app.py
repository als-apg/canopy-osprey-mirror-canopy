import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from canopy.hare import gateway as hare_gateway
from canopy.vole import gateway
from canopy.core.clock import SystemClock
from canopy.core.errors import register_canopy_error_handler
from canopy.vole.config import get_vole_config
from canopy.vole.runtime import mark_started
from canopy.vole.buffer.store import PVStore
from canopy.vole.api.v1.router import router as v1_router
from canopy.vole.services.notifier import ChangeNotifier
from canopy.vole.services.hare_ingest import HareIngestService
from canopy.vole.services.buffer_service import BufferService
from canopy.vole.services.eviction_service import EvictionService
from canopy.vole.services.alignment_service import AlignmentService

logger = logging.getLogger(__name__)


@asynccontextmanager
async def vole_lifespan():
    """Startup: construct the buffer service and bind to the gateway.

    Composed by the parent app's lifespan in ``canopy.main`` — mounted sub-apps
    don't run their own lifespan automatically behind ``app.mount()``.
    """
    config = get_vole_config()
    clock = SystemClock()
    store = PVStore(config)
    alignment = AlignmentService()

    ingest: HareIngestService | None = None

    async def _on_evict(pvs: list[str]) -> None:
        nonlocal ingest
        if ingest is not None:
            await ingest.unsubscribe(pvs)

    eviction = EvictionService(store, config, on_evict=_on_evict)
    notifier = ChangeNotifier()
    service = BufferService(store, alignment, eviction, clock, config, notifier=notifier)
    gateway.bind(service)
    gateway.bind_notifier(notifier)

    broker = hare_gateway.get_broker()
    ingest = HareIngestService(service, broker)
    await ingest.start()
    gateway.bind_ingest(ingest)
    logger.info("Vole connected to Hare for live data ingestion")

    mark_started()
    logger.info(
        "Vole started: max_points=%d, global_bytes_max=%d",
        config.max_points,
        config.global_bytes_max,
    )
    try:
        yield
    finally:
        await ingest.stop()
        gateway.bind_ingest(None)
        gateway.bind_notifier(None)
        gateway.bind(None)
        logger.info("Vole shutdown complete")


app = FastAPI(
    title="Canopy / Vole",
    description="In-memory time-aligned PV buffer.",
)

register_canopy_error_handler(app)
app.include_router(v1_router)
