from fastapi import Security

from canopy.core.health import make_health_router
from canopy.hare.runtime import uptime_s
from canopy.core.security import require_read_access
from canopy.hare.dependencies import PVBrokerDep
from canopy.hare.schemas.health import HealthSummaryDTO

router = make_health_router("hare", uptime_s)


@router.get("/summary", dependencies=[Security(require_read_access)])
async def summary(broker: PVBrokerDep) -> HealthSummaryDTO:
    """Broker rollup: backend, active monitors, connected clients, subscriptions."""
    status = broker.status()
    return HealthSummaryDTO(
        status="ok" if status["running"] else "degraded",
        backend=status.get("backend") or "unknown",
        connector_ok=bool(status["running"]),
        clients=status["clients"],
        active_monitors=status["active_monitors"],
        subscriptions=status["subscriptions"],
    )
