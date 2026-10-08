import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from canopy.converter import gateway
from canopy.core.errors import register_canopy_error_handler
from canopy.converter.config import get_converter_config
from canopy.converter.runtime import mark_started
from canopy.converter.api.v1.router import router as v1_router
from canopy.converter.adapters.edm_env import apply_edm_colors
from canopy.converter.services.conversion_service import ConversionService

logger = logging.getLogger(__name__)


@asynccontextmanager
async def converter_lifespan():
    """Startup: point the EDM parser at colors.list, construct the service, bind
    the gateway.

    Composed by the parent app's lifespan in ``canopy.main`` — mounted sub-apps
    do not run their own lifespan automatically behind ``app.mount()``.

    The converter is stateless (parse -> build -> emit), so the only startup
    state is the ConversionService singleton; its widget mappings come from the
    registry bundled with ``pydmconverter``. The worker process builds its own
    service instead (see :mod:`canopy.converter.tasks`).
    """
    config = get_converter_config()
    # Point the EDM parser at the bundled (or configured) colors.list.
    # TODO(pydm-converter-tool#150 + dep bump): replace with an explicit
    # color_list_path=config.colors_list_path argument to convert_bytes.
    apply_edm_colors(config)
    service = ConversionService()
    gateway.bind(service)
    mark_started()
    logger.info(
        "Converter started: colors_list=%s",
        config.colors_list_path,
    )
    try:
        yield
    finally:
        gateway.bind(None)
        logger.info("Converter shutdown complete")


app = FastAPI(
    title="Canopy / Converter",
    description="Legacy screen converter — .edl/.ui to Canopy Screen IR.",
)

register_canopy_error_handler(app)
app.include_router(v1_router)
