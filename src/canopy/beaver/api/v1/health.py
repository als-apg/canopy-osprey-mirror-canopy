from fastapi import Security

from canopy.core.health import make_health_router
from canopy.core.security import require_read_access
from canopy.beaver.runtime import uptime_s
from canopy.beaver.dependencies import WidgetServiceDep
from canopy.beaver.schemas.health import HealthSummaryDTO

router = make_health_router("beaver", uptime_s)


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
