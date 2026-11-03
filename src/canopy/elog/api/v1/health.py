from fastapi import Security

from canopy.core.health import make_health_router
from canopy.elog.runtime import uptime_s
from canopy.core.security import require_read_access
from canopy.elog.dependencies import ElogServiceDep
from canopy.elog.schemas.health import ElogHealthSummaryDTO

router = make_health_router("elog", uptime_s)


@router.get("/summary", dependencies=[Security(require_read_access)])
async def summary(service: ElogServiceDep) -> ElogHealthSummaryDTO:
    """Per-app token probe (``GET /v1/users/me`` on elog-plus) with concurrent fan-out (2s per probe)."""
    return await service.health_summary()
