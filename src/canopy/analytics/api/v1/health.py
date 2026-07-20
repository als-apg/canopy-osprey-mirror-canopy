import time

from fastapi import APIRouter

from canopy import __version__
from canopy.analytics.runtime import uptime_s
from canopy.analytics.schemas.health import HeartbeatDTO

router = APIRouter(prefix="/health", tags=["Analytics / Health"])


@router.get("/watchdog")
async def watchdog() -> HeartbeatDTO:
    """Liveness heartbeat."""
    return HeartbeatDTO(
        module="analytics",
        version=__version__,
        uptime_s=uptime_s(),
        now_ts_ns=time.time_ns(),
    )
