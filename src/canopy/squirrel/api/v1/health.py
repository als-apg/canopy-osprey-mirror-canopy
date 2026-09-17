"""Squirrel health endpoints: liveness and restore-hook configuration diagnostics."""

from fastapi import Security

from canopy.core.health import make_health_router
from canopy.core.security import require_read_access
from canopy.squirrel.runtime import uptime_s
from canopy.squirrel.restore_hooks import describe_restore_hooks
from canopy.squirrel.schemas.health import RestoreHooksStatusDTO

router = make_health_router("squirrel", uptime_s)


@router.get("/restore-hooks", dependencies=[Security(require_read_access)])
async def restore_hooks_status() -> RestoreHooksStatusDTO:
    """Report which restore hooks this process will run.

    Restore hooks send extra per-PV writes during a snapshot restore (magnet trims,
    for instance) and are selected with ``CANOPY_SQUIRREL__RESTORE_HOOKS``. A variable
    that never reaches the container, or a mistyped provider key, leaves the hook
    silently doing nothing — this endpoint makes that visible without running a
    restore. Reads configuration only: no hook is built or cached.

    Returns
    -------
    RestoreHooksStatusDTO
        The configured provider keys alongside those that resolved to a registered
        provider, those that did not, and everything available.
    """
    return RestoreHooksStatusDTO(**describe_restore_hooks())
