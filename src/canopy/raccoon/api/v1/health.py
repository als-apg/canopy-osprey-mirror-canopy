import time

from fastapi import APIRouter

from canopy import __version__
from canopy.raccoon.runtime import uptime_s
from canopy.raccoon.schemas.health import HeartbeatDTO

router = APIRouter(prefix="/health", tags=["Raccoon / Health"])


@router.get("/watchdog")
async def watchdog() -> HeartbeatDTO:
    """Liveness heartbeat. Does not call any adapter."""
    return HeartbeatDTO(
        module="raccoon",
        version=__version__,
        uptime_s=uptime_s(),
        now_ts_ns=time.time_ns(),
    )
