from canopy.contracts.base import CanopyModel


class PerPVInfoDTO(CanopyModel):
    pv: str
    point_count: int
    byte_estimate: int
    oldest_ts_ns: int | None
    newest_ts_ns: int | None
    last_access_ns: int


class VoleStatsDTO(CanopyModel):
    ring_count: int
    total_points: int
    total_bytes: int
    global_bytes_max: int
    pressure: bool
    rings: list[PerPVInfoDTO] = []


class HeartbeatDTO(CanopyModel):
    module: str
    version: str
    uptime_s: float
    now_ts_ns: int
    ring_count: int
    total_points: int
    total_bytes: int
