import time

from fastapi import Security, APIRouter

from canopy import __version__
from canopy.hare.runtime import uptime_s
from canopy.core.security import require_read_access
from canopy.hare.dependencies import PVBrokerDep
from canopy.hare.schemas.health import HeartbeatDTO, HealthSummaryDTO

router = APIRouter(prefix="/health", tags=["Hare / Health"])


@router.get("/watchdog")
async def watchdog() -> HeartbeatDTO:
    """Liveness heartbeat. Does not touch the connector."""
    return HeartbeatDTO(
        module="hare",
        version=__version__,
        uptime_s=uptime_s(),
        now_ts_ns=time.time_ns(),
    )


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
