from canopy.contracts.base import CanopyModel


class HeartbeatDTO(CanopyModel):
    """Per-module heartbeat returned by `/health/watchdog`.

    Used by liveness/readiness probes; does not call any adapter.
    """

    module: str
    version: str
    uptime_s: float
    now_ts_ns: int
