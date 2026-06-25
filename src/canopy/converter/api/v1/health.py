import time

from fastapi import APIRouter

from canopy import __version__
from canopy.converter.runtime import uptime_s
from canopy.converter.schemas.health import HeartbeatDTO

router = APIRouter(prefix="/health", tags=["Converter / Health"])


@router.get("/watchdog")
async def watchdog() -> HeartbeatDTO:
    """Liveness heartbeat. Does not call any external resource."""
    return HeartbeatDTO(
        module="converter",
        version=__version__,
        uptime_s=uptime_s(),
        now_ts_ns=time.time_ns(),
    )
