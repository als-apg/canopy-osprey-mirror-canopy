from typing import Literal

from canopy.contracts.base import CanopyModel


class HealthSummaryDTO(CanopyModel):
    """Broker health rollup returned by ``/health/summary``."""

    status: Literal["ok", "degraded"]
    backend: str
    connector_ok: bool
    clients: int
    active_monitors: int
    # Of active_monitors, those with no subscriber kept warm for the grace period.
    warm_monitors: int = 0
    subscriptions: int
    # The connector's identity, as in the heartbeat frame; null when it has none.
    label: str | None = None
    notice: str | None = None
    binding: str | None = None
