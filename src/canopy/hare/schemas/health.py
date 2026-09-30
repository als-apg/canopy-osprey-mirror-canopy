from typing import Literal

from canopy.contracts.base import CanopyModel


class HealthSummaryDTO(CanopyModel):
    """Broker health rollup returned by ``/health/summary``."""

    status: Literal["ok", "degraded"]
    backend: str
    connector_ok: bool
    clients: int
    active_monitors: int
    subscriptions: int
