"""EPICS connector contract.

`PVConnector` is the swappable interface Hare depends on. Currently the two built-in backends
are `simulation` (data all simulated in process without any dependencies) and `epics`
(real CA/PVA via aioca/p4p).

`canopy.epics.gateway` picks between them from the `CANOPY_EPICS__BACKEND` config value.
"""

from typing import Protocol, runtime_checkable
from collections.abc import Callable

from canopy.core.errors import ErrorCode, CanopyError
from canopy.contracts.pv import PVMeta, PVValue, PVWriteValue
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


ALARM_STATUS_CODES: dict[str, int] = {
    name: code
    for code, name in enumerate(
        (
            "NO_ALARM",
            "READ",
            "WRITE",
            "HIHI",
            "HIGH",
            "LOLO",
            "LOW",
            "STATE",
            "COS",
            "COMM",
            "TIMEOUT",
            "HWLIMIT",
            "CALC",
            "SCAN",
            "LINK",
            "SOFT",
            "BAD_SUB",
            "UDF",
            "DISABLE",
            "SIMM",
            "READ_ACCESS",
            "WRITE_ACCESS",
        )
    )
}
"""EPICS ``menuAlarmStat`` choice names mapped to their alarm status codes.

This is the code set Channel Access reports as a value's ``status`` and the one
``PVValue.alarm_status`` carries, whatever the protocol.
"""


def alarm_status_from_name(name: object) -> int | None:
    """Map an EPICS alarm status name to its ``menuAlarmStat`` code.

    PV Access reports the ``menuAlarmStat`` name (``"HIHI"``) in ``alarm.message``,
    because its own ``alarm.status`` is a coarser category (DEVICE, DRIVER, RECORD,
    ...) rather than the Channel Access code.

    Parameters
    ----------
    name : object
        The status name, matched case-insensitively. The C constant spelling with
        an ``_ALARM`` suffix (``"HIHI_ALARM"``) is accepted too.

    Returns
    -------
    int | None
        The ``menuAlarmStat`` code, or ``None`` when ``name`` is not a string naming
        a known status, such as a free-text alarm message.
    """
    if not isinstance(name, str):
        return None
    key = name.strip().upper()
    if key in ALARM_STATUS_CODES:
        return ALARM_STATUS_CODES[key]
    return ALARM_STATUS_CODES.get(key.removesuffix("_ALARM"))


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

    async def put(self, pv: str, value: PVWriteValue, *, timeout: float | None = None, wait: bool = True) -> None:
        """Write `value` to `pv`. When `wait=True`, block until the write
        completes (put-with-callback) so the caller knows it landed. The
        protocol is taken from the PV name prefix (``ca://`` / ``pva://``)"""
        ...

    async def subscribe(self, pv: str, on_event: EventCallback) -> Subscription:
        """Start monitoring `pv`; `on_event` fires on connect and each update."""
        ...

    async def describe(self, pv: str) -> PVMeta:
        """Static per-channel metadata (value kind, units, precision, enum
        choices, display/control limits). Read once per active PV by the broker
        and served by the `describe` endpoint."""
        ...
