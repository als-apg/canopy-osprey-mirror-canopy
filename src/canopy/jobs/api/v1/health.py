import time

from fastapi import APIRouter

from canopy import __version__
from canopy.jobs.runtime import uptime_s
from canopy.jobs.schemas.health import HeartbeatDTO

router = APIRouter(prefix="/health", tags=["Jobs / Health"])


@router.get("/watchdog")
async def watchdog() -> HeartbeatDTO:
    """Liveness heartbeat. Does not touch Redis."""
    return HeartbeatDTO(module="jobs", version=__version__, uptime_s=uptime_s(), now_ts_ns=time.time_ns())
