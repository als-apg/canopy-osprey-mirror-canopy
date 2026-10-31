"""Fox wire DTOs for the formula endpoints."""

from pydantic import Field

from canopy.contracts.base import CanopyModel
from canopy.contracts.sample import AlarmSeverity
from canopy.contracts.formula import (
    FormulaKind,
    FormulaSpec,
    TableHistory,
    TableInputState,
)


class EvaluateRequestDTO(CanopyModel):
    """Request body for ``POST /fox/v1/evaluate``."""

    spec: FormulaSpec
    t0_ns: int = Field(description="Window start in epoch nanoseconds")
    t1_ns: int = Field(description="Window end in epoch nanoseconds; 0 means 'now'")


class ScalarResultDTO(CanopyModel):
    ts_ns: int
    value: float | int | bool | str
    severity: AlarmSeverity
    inputs_used: dict[str, int]


class TimeseriesResultDTO(CanopyModel):
    ts_ns: list[int]
    values: list[float | None]
    severity: list[AlarmSeverity]
    inputs: dict[str, str]
    decimated: bool


class WaveformResultDTO(CanopyModel):
    ts_ns: int
    values: list[float]
    severity: AlarmSeverity


class TableInputDTO(CanopyModel):
    """Health of one table input, keyed by binding name in ``TableResultDTO.inputs``."""

    history: TableHistory
    status: TableInputState
    late_after_final: bool = Field(
        description="True while this input arrived late or showed IOC clock skew in the last 60 s"
    )


class TableResultDTO(CanopyModel):
    """A live table: shared row index, one float or null per column per row."""

    ts_ns: list[int]
    columns: dict[str, list[float | None]]
    inputs: dict[str, TableInputDTO]
    total_rows: int


class FormulaResultDTO(CanopyModel):
    """Discriminated union by ``kind``."""

    kind: FormulaKind
    scalar: ScalarResultDTO | None = None
    timeseries: TimeseriesResultDTO | None = None
    waveform: WaveformResultDTO | None = None
    table: TableResultDTO | None = None


class FormulaValidationDTO(CanopyModel):
    """Response from ``POST /fox/v1/validate``."""

    valid: bool
    tokens: list[str] = []
    errors: list[str] = []
