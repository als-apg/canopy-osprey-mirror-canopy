from typing import Literal

from canopy.contracts.base import CanopyModel


class ArchiverInfoDTO(CanopyModel):
    """One configured archiver instance with current health.

    Returned in the list at `GET /mole/v1/archivers`.
    """

    name: str
    type_name: str
    url: str
    healthy: bool
    latency_ms: float | None = None
    last_error: str | None = None


class ArchiverListDTO(CanopyModel):
    default: str
    archivers: list[ArchiverInfoDTO]


class ArchiverHealthDTO(CanopyModel):
    name: str
    healthy: bool
    latency_ms: float | None = None
    last_error: str | None = None
    last_check_ts_ns: int


class HealthSummaryDTO(CanopyModel):
    """Per-archiver health rollup.

    `status` is `"ok"` when all archivers are healthy (or none configured),
    `"degraded"` when at least one is unhealthy, `"down"` when all are.
    """

    status: Literal["ok", "degraded", "down"]
    archivers: list[ArchiverHealthDTO]
