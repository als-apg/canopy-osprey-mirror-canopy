import time

from fastapi import Security, APIRouter

from canopy import __version__
from canopy.core.security import require_read_access
from canopy.raccoon.runtime import uptime_s
from canopy.raccoon.dependencies import DirectoryServiceDep
from canopy.raccoon.schemas.health import HeartbeatDTO
from canopy.raccoon.schemas.directory import RaccoonStatsDTO

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


@router.get("/summary", dependencies=[Security(require_read_access)])
async def summary(service: DirectoryServiceDep) -> RaccoonStatsDTO:
    """Adapter health plus cache hit-rate / size rollup."""
    return await service.stats()
