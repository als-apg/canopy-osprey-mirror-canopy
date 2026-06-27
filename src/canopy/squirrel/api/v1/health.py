import time

from fastapi import APIRouter

from canopy import __version__
from canopy.squirrel.runtime import uptime_s
from canopy.squirrel.schemas.health import HeartbeatDTO

router = APIRouter(prefix="/health", tags=["Squirrel / Health"])


@router.get("/watchdog")
async def watchdog() -> HeartbeatDTO:
    """Liveness heartbeat. Does not touch the database."""
    return HeartbeatDTO(
        module="squirrel",
        version=__version__,
        uptime_s=uptime_s(),
        now_ts_ns=time.time_ns(),
    )
