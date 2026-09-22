from canopy.contracts.base import CanopyModel


class HeartbeatDTO(CanopyModel):
    module: str
    version: str
    uptime_s: float
    now_ts_ns: int
