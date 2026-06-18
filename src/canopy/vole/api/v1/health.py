import time

from fastapi import Security, APIRouter

from canopy.vole.runtime import uptime_s
from canopy.core.security import require_read_access
from canopy.vole.dependencies import BufferServiceDep
from canopy.vole.schemas.buffer import HeartbeatDTO, VoleStatsDTO

router = APIRouter(prefix="/health", tags=["Vole / Health"])


@router.get("/watchdog")
async def watchdog(service: BufferServiceDep) -> HeartbeatDTO:
    stats = await service.stats()
    return HeartbeatDTO(
        module="vole",
        version="0.1.0",
        uptime_s=uptime_s(),
        now_ts_ns=time.time_ns(),
        ring_count=stats.ring_count,
        total_points=stats.total_points,
        total_bytes=stats.total_bytes,
    )


@router.get(
    "/summary",
    dependencies=[Security(require_read_access)],
)
async def summary(service: BufferServiceDep) -> VoleStatsDTO:
    return await service.stats()
