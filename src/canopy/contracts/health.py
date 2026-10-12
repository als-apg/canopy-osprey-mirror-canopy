from typing import Literal

from canopy.contracts.base import CanopyModel


class HeartbeatDTO(CanopyModel):
    """Per-module heartbeat returned by a module's ``/health/watchdog`` endpoint.

    Shared across sub-modules via ``canopy.core.health.make_health_router``.
    Liveness/readiness probes only; the base fields never touch a module's
    backing store or adapters. A module whose watchdog needs extra fields
    (e.g. Vole, which reports buffer stats) defines its own DTO instead.
    """

    module: str
    version: str
    uptime_s: float
    now_ts_ns: int


class HareHealthSummaryDTO(CanopyModel):
    """Hare's broker rollup, returned by ``/hare/v1/health/summary`` and read by canopy_mcp."""

    status: Literal["ok", "degraded"]
    backend: str
    connector_ok: bool
    clients: int
    active_monitors: int
    # Of active_monitors, those whose latest value is connected.
    connected: int
    # Of active_monitors, those with no subscriber kept warm for the grace period.
    warm_monitors: int = 0
    subscriptions: int
    # The connector's identity, as in the heartbeat frame; null when it has none.
    label: str | None = None
    notice: str | None = None
    binding: str | None = None
