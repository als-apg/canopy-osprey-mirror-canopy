import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from canopy.fox import gateway
from canopy.mole import gateway as mole_gateway
from canopy.vole import gateway as vole_gateway
from canopy.core.clock import SystemClock
from canopy.fox.config import get_fox_config
from canopy.core.errors import register_canopy_error_handler
from canopy.fox.runtime import mark_started
from canopy.vole.adapter import VoleDataSource
from canopy.fox.api.v1.router import router as v1_router
from canopy.fox.services.parser import Parser
from canopy.fox.services.formula_service import FormulaService
from canopy.fox.services.subscription_manager import SubscriptionManager

logger = logging.getLogger(__name__)


@asynccontextmanager
async def fox_lifespan():
    """Startup: build the parser, construct the service, bind to the gateway.

    Composed by the parent app's lifespan in ``canopy.main`` — mounted sub-apps
    don't run their own lifespan automatically behind ``app.mount()``.
    """
    config = get_fox_config()
    data_source = VoleDataSource(
        vole_gateway.get_service(),
        fetch_window=mole_gateway.fetch_window,
        fetch_binned=mole_gateway.fetch_binned,
        subscribe_live=vole_gateway.subscribe_live,
        clock=SystemClock(),
    )
    parser = Parser(max_expression_len=config.max_expression_len)
    service = FormulaService(data_source, parser, config)
    gateway.bind(service)

    sub_manager = SubscriptionManager(
        formula_service=service,
        on_change_fn=vole_gateway.on_change,
        subscribe_live_fn=vole_gateway.subscribe_live,
    )
    gateway.bind_subscription_manager(sub_manager)

    mark_started()
    logger.info(
        "Fox started: eval_timeout=%dms, max_output_points=%d", config.eval_timeout_ms, config.max_output_points
    )
    try:
        yield
    finally:
        await sub_manager.shutdown()
        gateway.bind_subscription_manager(None)
        gateway.bind(None)
        logger.info("Fox shutdown complete")


app = FastAPI(
    title="Canopy / Fox",
    description="Formula engine — expression evaluation over PV samples.",
)

register_canopy_error_handler(app)
app.include_router(v1_router)
