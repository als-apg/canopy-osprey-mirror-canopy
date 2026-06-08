"""Shared live-PV value contract.

Emitted by the EPICS connector (`canopy.epics`), cached and fanned out by the
Hare broker, and sent to clients over WebSocket / REST. Other modules that need
a live value speak this type via `canopy.hare.gateway`.
"""

from canopy.contracts.base import CanopyModel
from canopy.contracts.sample import AlarmSeverity

# A live value is a scalar or a waveform (array). `None` when disconnected.
PVValueType = float | int | bool | str | list | None


class PVValue(CanopyModel):
    """A single live value of a PV, with alarm + connection metadata."""

    pv: str
    value: PVValueType
    severity: AlarmSeverity
    ts_ns: int
    connected: bool = True
    units: str | None = None
    precision: int | None = None
    error: str | None = None
