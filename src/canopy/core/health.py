"""Shared ``/health/watchdog`` router factory.

Every request-scoped Canopy sub-module exposes an identical liveness heartbeat
route; this factory builds that route once instead of each module hand-rolling
the same handler. Modules with additional health endpoints (e.g.
``/health/summary``) add them to the returned router afterward — it's a plain
``APIRouter``, so further ``@router.get(...)`` registrations work normally.
"""

import time
from collections.abc import Callable

from fastapi import APIRouter

from canopy import __version__
from canopy.contracts.health import HeartbeatDTO


def make_health_router(module: str, uptime_s: Callable[[], float]) -> APIRouter:
    """Build a ``/health`` router pre-populated with the ``/watchdog`` route.

    ``module`` names the sub-module (used in the DTO and the router's tag) and
    ``uptime_s`` is that module's ``ModuleRuntime.uptime_s`` (or the thin
    module-level wrapper around it).
    """
    router = APIRouter(prefix="/health", tags=[f"{module.capitalize()} / Health"])

    @router.get("/watchdog")
    async def watchdog() -> HeartbeatDTO:
        """Liveness heartbeat. Does not touch the database or any adapter."""
        return HeartbeatDTO(module=module, version=__version__, uptime_s=uptime_s(), now_ts_ns=time.time_ns())

    return router
