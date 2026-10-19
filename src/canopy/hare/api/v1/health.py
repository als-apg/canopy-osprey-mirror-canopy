import logging
from typing import Any

from fastapi import Security
from pydantic import ValidationError

from canopy.core.health import make_health_router
from canopy.hare.runtime import uptime_s
from canopy.core.security import require_read_access
from canopy.contracts.health import WritesDTO, ReachableDTO, HareHealthSummaryDTO
from canopy.hare.dependencies import PVBrokerDep

logger = logging.getLogger(__name__)

router = make_health_router("hare", uptime_s)

# The last malformed statement logged, per field, so a connector repeating one bad
# statement on every poll logs it once.
_last_malformed: dict[str, Any] = {}


def _statement(frame: dict, field: str, model: type[ReachableDTO] | type[WritesDTO]) -> Any:
    """The frame's ``field`` statement parsed by ``model``; null when absent or malformed.

    A malformed statement is no claim: the summary keeps answering, ``status`` falls
    back to the count rule, and the statement is logged once until it changes.
    """
    raw = frame.get(field)
    if raw is None:
        return None
    try:
        parsed = model.model_validate(raw)
    except ValidationError as exc:
        if _last_malformed.get(field) != raw:
            _last_malformed[field] = raw
            logger.warning("connector %s statement is malformed, treated as no claim: %r (%s)", field, raw, exc)
        return None
    _last_malformed.pop(field, None)
    return parsed


@router.get("/summary", dependencies=[Security(require_read_access)])
async def summary(broker: PVBrokerDep) -> HareHealthSummaryDTO:
    """Broker rollup: backend, its identity and statement, monitors, connected channels, clients, subscriptions.

    ``status`` is ``degraded`` when the broker is not running or the connector says the
    machine does not answer (``reachable.state == "no"``). With no ``reachable`` claim, it
    is ``degraded`` when channels are monitored and none is connected; a malformed
    statement counts as no claim. ``writes`` is reported, never judged.
    """
    status = broker.status()
    frame = broker.heartbeat_frame()
    running = bool(status["running"])
    monitored, connected = status["active_monitors"], status["connected"]
    reachable = _statement(frame, "reachable", ReachableDTO)
    writes = _statement(frame, "writes", WritesDTO)
    if reachable is not None:
        answers = reachable.state != "no"
    else:
        answers = not (monitored > 0 and connected == 0)
    return HareHealthSummaryDTO(
        status="ok" if running and answers else "degraded",
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
        reachable=reachable,
        writes=writes,
    )
