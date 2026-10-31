import logging
from typing import Any

from fastapi import Request, Security
from pydantic import ValidationError

from canopy.core.health import make_health_router
from canopy.hare.runtime import uptime_s
from canopy.core.security import require_read_access
from canopy.contracts.health import WritesDTO, ReachableDTO, HareHealthSummaryDTO
from canopy.hare.dependencies import PVBrokerDep

logger = logging.getLogger(__name__)

router = make_health_router("hare", uptime_s)

# Name of the app-state attribute holding the last malformed statement logged, per
# field, so a connector repeating one bad statement on every poll logs it once.
_MALFORMED_STATE = "hare_malformed_statements"


def _malformed_seen(request: Request) -> dict[str, Any]:
    """This app's record of the malformed statements already logged."""
    seen = getattr(request.app.state, _MALFORMED_STATE, None)
    if seen is None:
        seen = {}
        setattr(request.app.state, _MALFORMED_STATE, seen)
    return seen


def _statement(
    frame: dict, field: str, model: type[ReachableDTO] | type[WritesDTO], seen: dict[str, Any]
) -> ReachableDTO | WritesDTO | None:
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
        if seen.get(field) != raw:
            seen[field] = raw
            logger.warning("connector %s statement is malformed, treated as no claim: %r (%s)", field, raw, exc)
        return None
    seen.pop(field, None)
    return parsed


@router.get("/summary", dependencies=[Security(require_read_access)])
async def summary(request: Request, broker: PVBrokerDep) -> HareHealthSummaryDTO:
    """Broker rollup: backend, the connector's identity and statement, monitors, clients, subscriptions.

    ``status`` is ``degraded`` when the broker is not running or the connector says the
    control system does not answer (``reachable.state == "no"``). With no ``reachable``
    claim, it is ``degraded`` when channels are monitored and none is connected; a
    malformed statement counts as no claim. ``writes`` is reported, never judged.
    """
    status = broker.status()
    frame = broker.heartbeat_frame()
    seen = _malformed_seen(request)
    running = bool(status["running"])
    monitored, connected = status["active_monitors"], status["connected"]
    reachable = _statement(frame, "reachable", ReachableDTO, seen)
    writes = _statement(frame, "writes", WritesDTO, seen)
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
