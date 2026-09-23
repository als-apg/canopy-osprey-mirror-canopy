"""Arq worker entrypoint - the ``canopy-worker`` process.

Same image as ``canopy-api``, different command::

    arq canopy.worker.WorkerSettings

Runs long operations (snapshot create/restore) off the request path. Each
persistent module contributes its task functions; ``on_startup`` builds the
process-wide resources a task needs - the EPICS connector - and stashes them in
the Arq context so every job reuses one connection set rather than rebuilding it.
"""

import logging

from arq.connections import RedisSettings

from canopy.core.db import dispose_engine
from canopy.core.config import get_settings
from canopy.core.logging import configure_logging
from canopy.epics.config import get_epics_config
from canopy.epics.gateway import build_connector
from canopy.squirrel.tasks import create_snapshot_task, restore_snapshot_task
from canopy.converter.tasks import convert_batch
from canopy.squirrel.restore_hooks import publish_worker_status

configure_logging()
logger = logging.getLogger(__name__)


async def ping(ctx: dict) -> str:
    """Liveness task — enqueue and poll it to confirm the worker is processing jobs."""
    return "pong"


async def on_startup(ctx: dict) -> None:
    """Build process-wide resources once when the worker starts.

    Also publishes which restore hooks this process resolved, since restores run here
    and not in the API; ``GET /squirrel/v1/health/restore-hooks`` reads it back.

    Parameters
    ----------
    ctx : dict
        Arq's worker context; ``ctx["redis"]`` is its Redis connection.

    Returns
    -------
    None
    """
    connector = build_connector(get_epics_config())
    await connector.start()
    ctx["epics"] = connector
    hooks = await publish_worker_status(ctx["redis"])
    logger.info("canopy-worker startup complete: EPICS connector ready, restore hooks %s", hooks["loaded"])


async def on_shutdown(ctx: dict) -> None:
    connector = ctx.get("epics")
    if connector is not None:
        await connector.close()
    await dispose_engine()
    logger.info("canopy-worker shutdown complete")


# Task functions registered with Arq. Each module appends its tasks here.
FUNCTIONS = [ping, convert_batch, create_snapshot_task, restore_snapshot_task]


class WorkerSettings:
    functions = FUNCTIONS
    on_startup = on_startup
    on_shutdown = on_shutdown
    redis_settings = RedisSettings.from_dsn(get_settings().redis_url)
    max_jobs = 10
    job_timeout = 600  # 10 min — large snapshot create/restore
    keep_result = 3600  # keep results 1h so clients can poll after completion
    max_tries = 3
