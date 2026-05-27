import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from canopy.raccoon import gateway
from canopy.core.errors import register_canopy_error_handler
from canopy.raccoon.config import get_raccoon_config
from canopy.raccoon.runtime import mark_started
from canopy.raccoon.api.v1.router import router as v1_router
from canopy.raccoon.cache.flight import SingleFlight
from canopy.raccoon.cache.name_cache import PVNameCache
from canopy.raccoon.cache.pattern_cache import PatternCache
from canopy.raccoon.adapters.registry import build_registry
from canopy.raccoon.services.directory_service import DirectoryService

logger = logging.getLogger(__name__)


@asynccontextmanager
async def raccoon_lifespan():
    """Startup: build the registry, construct the service, bind to the gateway.

    Composed by the parent app's lifespan in `canopy.main`. Mounted sub-apps
    do not run their own lifespan automatically behind `app.mount()`.
    """
    config = get_raccoon_config()
    registry = build_registry(config)

    cache_cfg = config.directory.cache if config.directory else None
    pattern_cache = PatternCache(
        max_entries=cache_cfg.max_pattern_entries if cache_cfg else 10000,
        ttl_s=cache_cfg.pattern_ttl_s if cache_cfg else 300.0,
    )
    name_cache = PVNameCache(
        max_entries=cache_cfg.max_exists_entries if cache_cfg else 100000,
        positive_ttl_s=cache_cfg.exists_ttl_s if cache_cfg else 1800.0,
        negative_ttl_s=cache_cfg.negative_ttl_s if cache_cfg else 60.0,
    )
    flight = SingleFlight()

    service = DirectoryService(registry, pattern_cache, name_cache, flight, config)
    gateway.bind(service)
    mark_started()
    if registry.adapter is not None:
        logger.info("Raccoon started: adapter=%r type=%r", registry.adapter.name, registry.adapter.type_name)
    else:
        logger.warning("Raccoon started without a configured directory adapter")
    try:
        yield
    finally:
        await registry.close()
        gateway.bind(None)
        logger.info("Raccoon shutdown complete")


app = FastAPI(
    title="Canopy / Raccoon",
    description="PV name-lookup proxy over the lab directory service.",
)

register_canopy_error_handler(app)
app.include_router(v1_router)
