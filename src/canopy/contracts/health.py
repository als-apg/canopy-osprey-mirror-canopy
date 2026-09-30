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
