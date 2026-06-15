import time

from fastapi import APIRouter

from canopy import __version__
from canopy.fox.runtime import uptime_s
from canopy.fox.schemas.health import HeartbeatDTO

router = APIRouter(prefix="/health", tags=["Fox / Health"])


@router.get("/watchdog")
async def watchdog() -> HeartbeatDTO:
    """Liveness heartbeat. Does not call any external resource."""
    return HeartbeatDTO(
        module="fox",
        version=__version__,
        uptime_s=uptime_s(),
        now_ts_ns=time.time_ns(),
    )
