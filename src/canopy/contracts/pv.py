"""Shared live-PV value contract.

Emitted by the EPICS connector (`canopy.epics`), cached and fanned out by the
Hare broker, and sent to clients over WebSocket / REST. Other modules that need
a live value speak this type via `canopy.hare.gateway`.

A PV's data is split into two payloads, matching how EPICS itself separates it:

- `PVValue` — the dynamic part that changes every update (value, alarm
  severity, timestamp, connection). Sent on every WebSocket frame.
- `PVMeta` — the static per-channel metadata (units, precision, enum
  choices, display/control limits, ...). Sent once on the `initial` frame and
  served by the `describe` endpoint

`ValueKind` is the type discriminator: the connector classifies each PV by its declared
EPICS type — the PV Access Normative-Type id or the Channel Access DBR type.
"""

from enum import StrEnum

from canopy.contracts.base import CanopyModel
from canopy.contracts.sample import AlarmSeverity


class ValueKind(StrEnum):
    """How to interpret a `PVValue.value`.

    `IMAGE` is reserved for NTNDArray, which will travel as a dedicated binary
    frame rather than JSON. `UNSUPPORTED` covers any recognized-but-not-
    yet-rendered Normative Type (NTTable, NTMatrix, ...).
    """

    SCALAR = "scalar"
    STRING = "string"
    ENUM = "enum"
    WAVEFORM = "waveform"
    IMAGE = "image"
    UNSUPPORTED = "unsupported"


# A live value is a scalar, a string, an enum index, or a 1-D waveform (list).
# `None` when disconnected. Images never travel as a `PVValue` (see ValueKind.IMAGE).
PVValueType = float | int | bool | str | list | None


class PVValue(CanopyModel):
    """A single live value of a PV — the dynamic, per-update payload."""

    pv: str
    value: PVValueType
    value_kind: ValueKind = ValueKind.SCALAR
    severity: AlarmSeverity
    ts_ns: int
    connected: bool = True
    error: str | None = None


class PVMeta(CanopyModel):
    """Static per-channel metadata — the bits a client reads once on connect to
    configure a display (units/precision/limits/enum choices).

    Sent on the `initial` WebSocket frame and by `GET /hare/v1/pvs/{pv}/describe`.
    All fields are optional. Whatever a backend can't supply stays `None`.
    """

    pv: str
    value_kind: ValueKind = ValueKind.SCALAR
    units: str | None = None
    precision: int | None = None
    description: str | None = None
    # Enum choice labels, indexed by the integer `PVValue.value` of an enum PV.
    enum_choices: list[str] | None = None
    # Display (view) range and control (writable) range, from EPICS CTRL metadata.
    display_low: float | None = None
    display_high: float | None = None
    control_low: float | None = None
    control_high: float | None = None
    writable: bool | None = None
    # Display format hint (e.g. "Decimal", "Hex"), from PVA display.form.
    form: str | None = None
