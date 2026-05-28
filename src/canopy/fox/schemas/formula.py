"""Fox wire DTOs for the formula endpoints."""

from pydantic import Field

from canopy.contracts.base import CanopyModel
from canopy.contracts.sample import AlarmSeverity
from canopy.contracts.formula import FormulaKind, FormulaSpec


class EvaluateRequestDTO(CanopyModel):
    """Request body for ``POST /fox/v1/evaluate``."""

    spec: FormulaSpec
    t0_ns: int = Field(description="Window start in epoch nanoseconds")
    t1_ns: int = Field(description="Window end in epoch nanoseconds; 0 means 'now'")


class ScalarResultDTO(CanopyModel):
    ts_ns: int
    value: float | int | bool
    severity: AlarmSeverity
    inputs_used: dict[str, int]


class CurveResultDTO(CanopyModel):
    ts_ns: list[int]
    values: list[float | None]
    severity: list[AlarmSeverity]
    inputs: dict[str, str]
    decimated: bool


class WaveformResultDTO(CanopyModel):
    ts_ns: int
    values: list[float]
    severity: AlarmSeverity


class FormulaResultDTO(CanopyModel):
    """Discriminated union by ``kind``."""

    kind: FormulaKind
    scalar: ScalarResultDTO | None = None
    curve: CurveResultDTO | None = None
    waveform: WaveformResultDTO | None = None


class FormulaValidationDTO(CanopyModel):
    """Response from ``POST /fox/v1/validate``."""

    valid: bool
    tokens: list[str] = []
    errors: list[str] = []
