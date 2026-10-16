import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from canopy.converter import gateway
from canopy.core.errors import register_canopy_error_handler
from canopy.converter.config import get_converter_config
from canopy.converter.runtime import mark_started
from canopy.converter.api.v1.router import router as v1_router
from canopy.converter.adapters.edm_lists import resolve_edm_lists
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
    registry bundled with ``pydmconverter``. Startup resolves the ``colors.list``
    and ``calc.list`` to hand the EDM parser, constructs the service and binds it
    to the gateway; shutdown unbinds it. The worker process builds its own
    service instead (see :mod:`canopy.converter.tasks`).

    Yields
    ------
    None
        Control while the converter is running with its service bound.
    """
    config = get_converter_config()
    colors_list, calc_list = resolve_edm_lists(config)
    service = ConversionService(
        colors_list_path=colors_list,
        calc_list_path=calc_list,
        search_paths=config.search_paths,
    )
    gateway.bind(service)
    mark_started()
    logger.info(
        "Converter started: colors_list=%s, calc_list=%s",
        colors_list,
        calc_list,
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
