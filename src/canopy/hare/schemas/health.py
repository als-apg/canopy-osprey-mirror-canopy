from typing import Literal

from canopy.contracts.base import CanopyModel


class HeartbeatDTO(CanopyModel):
    """Per-module heartbeat returned by ``/health/watchdog``. No auth, no I/O."""

    module: str
    version: str
    uptime_s: float
    now_ts_ns: int


class HealthSummaryDTO(CanopyModel):
    """Broker health rollup returned by ``/health/summary``."""

    status: Literal["ok", "degraded"]
    backend: str
    connector_ok: bool
    clients: int
    active_monitors: int
    subscriptions: int
