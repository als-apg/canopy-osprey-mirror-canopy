"""EPICS connector contract.

`PVConnector` is the swappable interface Hare depends on. Currently the two built-in backends
are `simulation` (data all simulated in process without any dependencies) and `epics`
(real CA/PVA via aioca/p4p).

`canopy.epics.gateway` picks between them from the `CANOPY_EPICS__BACKEND` config value.
The same value can instead name an out-of-tree backend by a `module:Class` dotted path.
"""

from typing import Any, Protocol, runtime_checkable
from collections.abc import Mapping, Callable

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
    """Contract every connector backend satisfies. See `docs` / Hare design.

    A backend may also implement `ConnectorStatement` to state its identity and
    the control system's state in Hare's heartbeat.
    """

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

    async def put(
        self,
        pv: str,
        value: PVWriteValue,
        *,
        timeout: float | None = None,
        wait: bool = True,
        binding: str | None = None,
        origin: Mapping[str, Any] | None = None,
        confirmed: bool = False,
    ) -> None:
        """Write `value` to `pv`. When `wait=True`, block until the write
        completes (put-with-callback) so the caller knows it landed. The
        protocol is taken from the PV name prefix (``ca://`` / ``pva://``).

        `binding`, `origin` and `confirmed` describe the write, as the client
        sent them; the first two are ``None`` when it sent none. `binding` is
        the connector binding the issuing screen was shown (the heartbeat's
        ``binding``), so a connector that can switch what it is connected to
        may refuse a write aimed at another one. `origin` is where the write
        came from, a plain ``{"screen": ..., "rev": ...}`` mapping, for the
        connector's own audit record. `confirmed` is true when a person
        confirmed the write in the viewer's dialog; a connector whose
        ``writes`` statement says ``asked`` refuses a write without it. A
        connector that has no use for any of them ignores them."""
        ...

    async def subscribe(self, pv: str, on_event: EventCallback) -> Subscription:
        """Start monitoring `pv`; `on_event` fires on connect and each update."""
        ...

    async def describe(self, pv: str) -> PVMeta:
        """Static per-channel metadata (value kind, units, precision, enum
        choices, display/control limits). Read once per active PV by the broker
        and served by the `describe` endpoint."""
        ...


@runtime_checkable
class ConnectorStatement(Protocol):
    """Optional seam: a connector that states what it is connected to and what it allows.

    The broker reads these attributes on every heartbeat frame (see
    ``PVBroker.heartbeat_frame``) and copies them into the frame and into
    ``GET /hare/v1/health/summary``. A connector opts in by providing all of
    them; one that does not is treated as making no statement, and every field
    reads ``None``. An attribute the connector has nothing to say about is
    ``None``, which a reader takes as "no claim".

    - ``label`` and ``notice``: a short name for what the connector is connected
      to, and a one-line remark a client may show next to it.
    - ``binding``: an id for that connection, which changes whenever the
      connector switches what it is connected to. A client sends it back with
      a write (``PUT`` body ``binding``) so the connector can refuse a write
      meant for another one.
    - ``reachable``: whether the control system answers, as
      ``{"state": "yes" | "no" | "unknown", "since": <epoch s> | None, "reason": str | None}``.
    - ``writes``: whether screens may write, as
      ``{"state": "on" | "asked" | "off" | "locked" | "applying" | "held", "reason": str | None, "cause": str | None}``;
      ``asked`` asks a person before each write (the PUT then carries
      ``confirmed``), ``held`` is a watchdog's hold on the session.

    The broker compares (label, notice, binding, reachable, writes) on every
    flush tick and sends the heartbeat at once when it changed, then calls
    `statement_sent`, so a connector can tell when its new statement is out.
    """

    label: str | None
    notice: str | None
    binding: str | None
    reachable: Mapping[str, Any] | None
    writes: Mapping[str, Any] | None

    def statement_sent(self) -> None:
        """Called after a changed statement was sent to every connected client. May do nothing."""
        ...
