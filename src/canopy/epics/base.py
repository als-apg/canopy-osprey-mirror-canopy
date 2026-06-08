"""EPICS connector contract.

`PVConnector` is the swappable interface Hare depends on. Currently the two built-in backends
are `simulation` (data all simulated in process without any dependencies) and `epics`
(real CA/PVA via aioca/p4p).

`canopy.epics.gateway` picks between them from the `CANOPY_EPICS__BACKEND` config value.
"""

from typing import Protocol, runtime_checkable
from collections.abc import Callable

from canopy.core.errors import ErrorCode, CanopyError
from canopy.contracts.pv import PVValue
from canopy.contracts.sample import AlarmSeverity

# --------------------------------------------------------------------------- #
# Domain exceptions — services raise these; the per-module handler maps them to
# {"code": <slug>, "detail": ...} JSON.
# --------------------------------------------------------------------------- #


class EpicsError(CanopyError):
    """Base for all EPICS-connector errors."""


class PVConnectionFailed(EpicsError):
    status_code = 503
    error_code = ErrorCode.PV_CONNECTION_FAILED


class PVReadTimeout(EpicsError):
    status_code = 504
    error_code = ErrorCode.PV_READ_TIMEOUT


class PVWriteFailed(EpicsError):
    status_code = 502
    error_code = ErrorCode.PV_WRITE_FAILED


class EpicsBackendUnavailable(EpicsError):
    """Raised when a configured backend cannot be loaded (e.g. aioca/p4p not installed)."""

    status_code = 503
    error_code = ErrorCode.EPICS_BACKEND_UNAVAILABLE


# --------------------------------------------------------------------------- #
# Severity normalization
# --------------------------------------------------------------------------- #

_SEVERITY_MAP: dict[int, AlarmSeverity] = {
    0: AlarmSeverity.NO_ALARM,
    1: AlarmSeverity.MINOR,
    2: AlarmSeverity.MAJOR,
    3: AlarmSeverity.INVALID,
}


def normalize_severity(value: int | None) -> AlarmSeverity:
    """Map an EPICS integer severity to Canopy's enum. Unknown/None -> INVALID."""
    if value is None:
        return AlarmSeverity.INVALID
    return _SEVERITY_MAP.get(int(value), AlarmSeverity.INVALID)


# --------------------------------------------------------------------------- #
# Connector contract
# --------------------------------------------------------------------------- #

# A value callback. MUST be non-blocking and is invoked on the asyncio event
# loop thread — backends that receive updates on other threads marshal onto the
# loop (e.g. loop.call_soon_threadsafe) before calling it.
EventCallback = Callable[[PVValue], None]


@runtime_checkable
class Subscription(Protocol):
    """Handle to one active monitor. Closed when the last subscriber leaves."""

    async def close(self) -> None: ...


@runtime_checkable
class PVConnector(Protocol):
    """Contract every connector backend satisfies. See `docs` / Hare design."""

    backend_name: str

    @classmethod
    def from_config(cls, cfg: "object") -> "PVConnector":
        """Build an instance from an `EpicsConfig`. Owns its own client(s)."""
        ...

    async def start(self) -> None:
        """Acquire resources / capture the running loop. Called at app startup."""
        ...

    async def close(self) -> None:
        """Drain clients and cancel monitors. Called at app shutdown."""
        ...

    async def get(self, pv: str, *, timeout: float | None = None) -> PVValue:
        """One-shot read of a PV's current value."""
        ...

    async def subscribe(self, pv: str, on_event: EventCallback) -> Subscription:
        """Start monitoring `pv`; `on_event` fires on connect and each update."""
        ...
