import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from canopy.converter import gateway
from canopy.core.errors import register_canopy_error_handler
from canopy.core.color_scheme import get_color_scheme
from canopy.converter.config import get_converter_config
from canopy.converter.runtime import mark_started
from canopy.converter.api.v1.router import router as v1_router
from canopy.converter.adapters.edm_env import apply_edm_colors
from canopy.converter.adapters.beaver_registry import build_registry
from canopy.converter.services.conversion_service import ConversionService

logger = logging.getLogger(__name__)


@asynccontextmanager
async def converter_lifespan():
    """Startup for the converter module.

    Composed by the parent app's lifespan in ``canopy.main`` — mounted sub-apps
    do not run their own lifespan automatically behind ``app.mount()``.

    The converter is stateless (parse -> build -> emit). Startup constructs the
    ConversionService (with its widget registry) and binds it to the gateway.
    """
    config = get_converter_config()
    # Point the EDM parser at the bundled (or configured) colors.list.
    # TODO(pydm-converter-tool#150 + dep bump): replace with an explicit
    # color_list_path=config.colors_list_path argument to convert_bytes.
    apply_edm_colors(config)
    # Seam #1: pick the widget registry by config — the Beaver gateway when
    # enabled, else the vendored snapshot shipped with pydmconverter.
    registry = build_registry(config)
    # The color scheme is flattened once per process (get_color_scheme caches per
    # path); the service receives the loaded result and never loads one itself.
    service = ConversionService(
        registry=registry,
        scheme=get_color_scheme(config.scheme_path),
        color_def_path=config.color_def_path,
    )
    gateway.bind(service)
    mark_started()
    logger.info(
        "Converter started: colors_list=%s, beaver_gateway=%s, scheme=%s, color_def=%s",
        config.colors_list_path,
        config.use_beaver_gateway,
        (service.scheme_meta or {}).get("served"),
        config.color_def_path,
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
