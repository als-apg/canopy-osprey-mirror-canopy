import time

from fastapi import Security, APIRouter

from canopy import __version__
from canopy.core.security import require_read_access
from canopy.beaver.runtime import uptime_s
from canopy.beaver.dependencies import WidgetServiceDep
from canopy.beaver.schemas.health import HeartbeatDTO, HealthSummaryDTO

router = APIRouter(prefix="/health", tags=["Beaver / Health"])


@router.get("/watchdog")
async def watchdog() -> HeartbeatDTO:
    """Liveness heartbeat. Does not touch the catalog."""
    return HeartbeatDTO(
        module="beaver",
        version=__version__,
        uptime_s=uptime_s(),
        now_ts_ns=time.time_ns(),
    )


@router.get("/summary", dependencies=[Security(require_read_access)])
async def summary(service: WidgetServiceDep) -> HealthSummaryDTO:
    """Catalog rollup: widget count, category count, registry version."""
    index = service.index
    return HealthSummaryDTO(
        status="ok" if index.widget_count > 0 else "degraded",
        widget_count=index.widget_count,
        category_count=index.category_count,
        registry_version=index.registry_version,
    )
