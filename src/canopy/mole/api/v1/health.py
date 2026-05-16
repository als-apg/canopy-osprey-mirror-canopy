import time

from fastapi import APIRouter

from canopy import __version__
from canopy.mole.runtime import uptime_s
from canopy.mole.schemas.health import HeartbeatDTO

router = APIRouter(prefix="/health", tags=["Mole / Health"])


@router.get("/watchdog")
async def watchdog() -> HeartbeatDTO:
    """Liveness heartbeat. Does not call any adapter."""
    return HeartbeatDTO(
        module="mole",
        version=__version__,
        uptime_s=uptime_s(),
        now_ts_ns=time.time_ns(),
    )
