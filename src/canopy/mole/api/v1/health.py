from fastapi import Security

from canopy.core.health import make_health_router
from canopy.mole.runtime import uptime_s
from canopy.core.security import require_read_access
from canopy.mole.dependencies import ArchiverServiceDep
from canopy.mole.schemas.archiver import HealthSummaryDTO

router = make_health_router("mole", uptime_s)


@router.get("/summary", dependencies=[Security(require_read_access)])
async def summary(service: ArchiverServiceDep) -> HealthSummaryDTO:
    """Per-archiver rollup with concurrent fan-out (2s per probe)."""
    return await service.health_summary()
