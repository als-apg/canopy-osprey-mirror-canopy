from fastapi import Security

from canopy.core.health import make_health_router
from canopy.core.security import require_read_access
from canopy.raccoon.runtime import uptime_s
from canopy.raccoon.dependencies import DirectoryServiceDep
from canopy.raccoon.schemas.directory import RaccoonStatsDTO

router = make_health_router("raccoon", uptime_s)


@router.get("/summary", dependencies=[Security(require_read_access)])
async def summary(service: DirectoryServiceDep) -> RaccoonStatsDTO:
    """Adapter health plus cache hit-rate / size rollup."""
    return await service.stats()
