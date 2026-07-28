import time

from fastapi import APIRouter

from canopy import __version__
from canopy.magpie.runtime import uptime_s
from canopy.magpie.schemas.health import HeartbeatDTO

router = APIRouter(prefix="/health", tags=["Magpie / Health"])


@router.get("/watchdog")
async def watchdog() -> HeartbeatDTO:
    """Liveness heartbeat. Does not touch the database."""
    return HeartbeatDTO(
        module="magpie",
        version=__version__,
        uptime_s=uptime_s(),
        now_ts_ns=time.time_ns(),
    )
