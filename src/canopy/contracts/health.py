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


class HealthDTO(CanopyModel):
    """``GET /health`` on canopy and canopy_mcp: the process answers.

    The body is the same constant on both servers; nothing in it is read from
    a file or from another process.
    """

    status: str

    @classmethod
    def of(cls) -> "HealthDTO":
        return cls(status="healthy")


# Why a panel server's rollup is degraded, beside canopy.core.color_scheme.DEGRADED_KEY.
DATA_UNAVAILABLE = "data-unavailable"
DATA_DEGRADED = "data-degraded"


class ChannelCountsDTO(CanopyModel):
    """Of the channels DATA monitors for open panels, how many are connected."""

    connected: int
    total: int


class FrontHealthSummaryDTO(CanopyModel):
    """``GET /health/summary`` on canopy_mcp, behind its access credential.

    ``degraded`` names every cause, and ``status`` is ``degraded`` exactly
    when it is non-empty. ``channels`` is null when there are no live
    channels (mock mode) or DATA's summary could not be read.
    """

    status: Literal["ok", "degraded"]
    degraded: list[str] = []
    channels: ChannelCountsDTO | None = None
