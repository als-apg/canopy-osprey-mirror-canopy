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

    `TABLE` carries a `TableValue` (PV Access NTTable). `IMAGE` is reserved for
    NTNDArray, which will travel as a dedicated binary frame rather than JSON.
    `UNSUPPORTED` covers any recognized-but-not-yet-rendered Normative Type
    (NTMatrix, NTHistogram, NTNameValue, ...).
    """

    SCALAR = "scalar"
    STRING = "string"
    ENUM = "enum"
    WAVEFORM = "waveform"
    TABLE = "table"
    IMAGE = "image"
    UNSUPPORTED = "unsupported"


class TableValue(CanopyModel):
    """A tabular value (PV Access NTTable), carried inline on a `PVValue` whose
    `valueKind` is `table`.

    Column-major and self-describing: `columns[i]` holds the cells of the column
    headed by `labels[i]`, and every column has the same length (the row count).
    Labels travel with the value (as Phoebus VTable does) because they are part of
    the NTTable structure itself, not separate per-channel display metadata.
    """

    labels: list[str]
    columns: list[list]


# A live value is a scalar, a string, an enum index, a 1-D waveform (list), or a
# `TableValue`. `None` when disconnected. Images never travel as a `PVValue`.
PVValueType = float | int | bool | str | list | TableValue | None

# The writable subset of `PVValueType`. The list element type is constrained to
# scalars so a malformed array (e.g. a list of objects) is rejected at the DTO
# boundary.
PVWriteValue = float | int | bool | str | list[float | int | bool | str]


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
    # Warning/alarm thresholds
    warning_low: float | None = None
    warning_high: float | None = None
    alarm_low: float | None = None
    alarm_high: float | None = None
    writable: bool | None = None
    # Display format hint (e.g. "Decimal", "Hex"), from PVA display.form.
    form: str | None = None
