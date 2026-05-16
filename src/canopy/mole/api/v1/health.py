import time

from fastapi import Security, APIRouter

from canopy import __version__
from canopy.core.security import require_read_access
from canopy.mole.runtime import uptime_s
from canopy.mole.schemas.health import HeartbeatDTO
from canopy.mole.schemas.archiver import HealthSummaryDTO
from canopy.mole.dependencies import ArchiverServiceDep

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


@router.get("/summary", dependencies=[Security(require_read_access)])
async def summary(service: ArchiverServiceDep) -> HealthSummaryDTO:
    """Per-archiver rollup with concurrent fan-out (2s per probe)."""
    return await service.health_summary()
