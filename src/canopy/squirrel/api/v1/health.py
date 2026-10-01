"""Squirrel health endpoints: liveness and restore-hook configuration diagnostics."""

import asyncio
from dataclasses import dataclass

from fastapi import Security
from redis.exceptions import RedisError

from canopy.jobs import gateway as jobs_gateway
from canopy.core.health import make_health_router
from canopy.core.security import require_read_access
from canopy.squirrel.runtime import uptime_s
from canopy.squirrel.restore_hooks import read_worker_status, describe_restore_hooks
from canopy.squirrel.schemas.health import (
    RestoreHooksViewDTO,
    RestoreHooksStatusDTO,
    WorkerRestoreHooksViewDTO,
)

router = make_health_router("squirrel", uptime_s)

WORKER_STATUS_TIMEOUT_S = 2.0


@dataclass(frozen=True)
class WorkerReport:
    """The outcome of reading the worker's published restore-hook report.

    Attributes
    ----------
    view : WorkerRestoreHooksViewDTO | None
        The report, or ``None`` when no worker has published one or Redis could not
        be read.
    error : str | None
        Why Redis could not be read within ``WORKER_STATUS_TIMEOUT_S``, else ``None``.
    """

    view: WorkerRestoreHooksViewDTO | None = None
    error: str | None = None


async def _worker_report() -> WorkerReport:
    """Fetch the worker's published restore-hook report from Redis.

    Returns
    -------
    WorkerReport
        The worker's view, or why it could not be read.
    """
    try:
        async with asyncio.timeout(WORKER_STATUS_TIMEOUT_S):
            report = await read_worker_status(await jobs_gateway.get_pool())
    except (TimeoutError, OSError, RedisError) as exc:
        return WorkerReport(error=f"Could not read the worker's report from Redis: {str(exc) or type(exc).__name__}")
    return WorkerReport(view=None if report is None else WorkerRestoreHooksViewDTO(**report))


@router.get("/restore-hooks", dependencies=[Security(require_read_access)])
async def restore_hooks_status() -> RestoreHooksStatusDTO:
    """Report which restore hooks the worker and this API process resolved.

    Restore hooks send extra per-PV writes during a snapshot restore (magnet trims,
    for instance) and are selected with ``CANOPY_SQUIRREL__RESTORE_HOOKS``. A variable
    that never reaches the worker, or a mistyped provider key, leaves the hook silently
    doing nothing — this endpoint makes that visible without running a restore.

    Restores run in ``canopy-worker``, so its view comes from the report it publishes
    to Redis at startup. The API's own view is computed here from configuration
    only: no hook is built or cached.

    Returns
    -------
    RestoreHooksStatusDTO
        Both views, whether they load the same hooks, and every available provider.
    """
    api = describe_restore_hooks()
    worker = await _worker_report()
    return RestoreHooksStatusDTO(
        available=api["available"],
        api=RestoreHooksViewDTO(**api),
        worker=worker.view,
        worker_error=worker.error,
        matches_api=None if worker.view is None else worker.view.loaded == api["loaded"],
    )
