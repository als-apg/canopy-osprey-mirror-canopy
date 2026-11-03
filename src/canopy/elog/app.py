import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from pydantic import ValidationError
from pydantic_settings import SettingsError

from canopy.elog import gateway
from canopy.core.errors import register_canopy_error_handler
from canopy.elog.config import ElogConfig, get_elog_config, describe_settings_error
from canopy.elog.runtime import mark_started
from canopy.elog.api.v1.router import router as v1_router
from canopy.elog.adapters.registry import ElogRegistry, build_registry
from canopy.elog.services.elog_service import ElogService

logger = logging.getLogger(__name__)


@asynccontextmanager
async def elog_lifespan():
    """Composed by the parent app's lifespan in ``canopy.main``.

    Mounted sub-apps don't run their own lifespan behind ``app.mount()``.
    Startup builds one adapter per configured app (each with its own token)
    and binds the ElogService to the gateway; shutdown closes the adapters.
    Settings that don't parse disable e-log rather than stopping Canopy: the
    service then runs on defaults and reports why in every app's config.
    """
    try:
        config = get_elog_config()
    except (ValidationError, SettingsError) as exc:
        reason = describe_settings_error(exc)
        logger.error("E-log disabled: %s", reason)
        config, registry = ElogConfig.model_construct(), ElogRegistry(None, disabled_reason=reason)
    else:
        registry = build_registry(config)
    service = ElogService(config, registry)
    gateway.bind(service)
    mark_started()
    if registry.enabled:
        logger.info(
            "E-log started: provider=%s, apps=%s", registry.provider, ",".join(registry.apps) or "(none with a token)"
        )
    else:
        logger.info("E-log started DISABLED: %s", registry.disabled_reason)
    try:
        yield
    finally:
        gateway.bind(None)
        await service.close()
        logger.info("E-log shutdown complete")


app = FastAPI(
    title="Canopy / E-log",
    description="Server-side e-log posting: one e-log application token per client app, so clients never hold one.",
)

register_canopy_error_handler(app)
app.include_router(v1_router)
