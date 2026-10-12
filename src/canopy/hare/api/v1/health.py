from fastapi import Security

from canopy.core.health import make_health_router
from canopy.hare.runtime import uptime_s
from canopy.core.security import require_read_access
from canopy.contracts.health import HareHealthSummaryDTO
from canopy.hare.dependencies import PVBrokerDep

router = make_health_router("hare", uptime_s)


@router.get("/summary", dependencies=[Security(require_read_access)])
async def summary(broker: PVBrokerDep) -> HareHealthSummaryDTO:
    """Broker rollup: backend and its identity, active monitors, connected channels, connected clients, subscriptions."""
    status = broker.status()
    frame = broker.heartbeat_frame()
    running = bool(status["running"])
    monitored, connected = status["active_monitors"], status["connected"]
    return HareHealthSummaryDTO(
        status="ok" if running and not (monitored > 0 and connected == 0) else "degraded",
        backend=status.get("backend") or "unknown",
        connector_ok=running,
        clients=status["clients"],
        active_monitors=monitored,
        connected=connected,
        warm_monitors=status.get("warm_monitors", 0),
        subscriptions=status["subscriptions"],
        label=frame["label"],
        notice=frame["notice"],
        binding=frame["binding"],
    )
