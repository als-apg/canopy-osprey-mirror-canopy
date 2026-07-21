from pydantic import Field, field_validator

from canopy.contracts.base import CanopyModel
from canopy.contracts.sample import Sample


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
    rings: list[PerPVInfoDTO] = Field(default_factory=list)


class PVSeriesDTO(CanopyModel):
    """Single-PV time-series read. Scalar values only; waveform/bytes PVs are not supported."""

    pv: str
    count: int
    samples: list[Sample] = Field(default_factory=list)

    @field_validator("samples", mode="before")
    @classmethod
    def _reject_bytes_values(cls, v: list) -> list:
        for s in v:
            val = s.value if hasattr(s, "value") else s.get("value")
            if isinstance(val, bytes):
                raise ValueError("bytes values are not supported in single-PV read; only scalar PVs are allowed")
        return v


class AlignedPVDTO(CanopyModel):
    pv: str
    values: list[float | None] = Field(default_factory=list)
    severities: list[int] = Field(default_factory=list)


class AlignedResultDTO(CanopyModel):
    ts_ns: list[int] = Field(default_factory=list)
    pvs: list[AlignedPVDTO] = Field(default_factory=list)


class HeartbeatDTO(CanopyModel):
    module: str
    version: str
    uptime_s: float
    now_ts_ns: int
    ring_count: int
    total_points: int
    total_bytes: int


class PVSubscribeRequest(CanopyModel):
    pvs: list[str]
