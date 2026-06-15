"""Shared formula domain models.

Used by Fox (the evaluator), and eventually Owl (persists formula definitions),
Squirrel (delta-vs-snapshot), and notebooks (ad-hoc evaluation).
"""

from typing import Literal

from canopy.contracts.base import CanopyModel
from canopy.contracts.sample import AlarmSeverity

FormulaKind = Literal["scalar", "timeseries", "waveform"]
AlignmentMode = Literal["last", "interpolate"]
IndexMode = Literal["union", "intersection", "grid", "first_input"]


class FormulaSpec(CanopyModel):
    """A complete formula request.

    Owl stores these in its plot configs; Fox accepts them as input.
    """

    kind: FormulaKind
    expression: str
    bindings: dict[str, str]
    align: AlignmentMode = "last"
    index: IndexMode = "union"
    grid_dt_ns: int | None = None
    max_points: int | None = None


class ScalarResult(CanopyModel):
    ts_ns: int
    value: float | int | bool
    severity: AlarmSeverity
    inputs_used: dict[str, int]


class TimeseriesResult(CanopyModel):
    ts_ns: list[int]
    values: list[float | None]
    severity: list[AlarmSeverity]
    inputs: dict[str, str]
    decimated: bool


class WaveformResult(CanopyModel):
    ts_ns: int
    values: list[float]
    severity: AlarmSeverity


class FormulaResult(CanopyModel):
    """Tagged union by `kind`. Frontend discriminates on this field."""

    kind: FormulaKind
    scalar: ScalarResult | None = None
    timeseries: TimeseriesResult | None = None
    waveform: WaveformResult | None = None


class FormulaValidationResult(CanopyModel):
    """Result of a parse+typecheck without evaluation."""

    valid: bool
    tokens: list[str] = []
    errors: list[str] = []
