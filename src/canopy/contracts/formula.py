"""Shared formula domain models.

Used by Fox (the evaluator), and eventually Owl (persists formula definitions),
Squirrel (delta-vs-snapshot), and notebooks (ad-hoc evaluation).
"""

from typing import Literal

from pydantic import Field, model_validator

from canopy.contracts.base import CanopyModel
from canopy.contracts.sample import AlarmSeverity

FormulaKind = Literal["scalar", "timeseries", "waveform", "table"]
FormulaMode = Literal["raw", "binned"]
AlignmentMode = Literal["last", "interpolate"]
IndexMode = Literal["union", "intersection", "grid", "first_input"]
TableHistory = Literal["seeded", "live-only", "not-archived", "truncated"]
TableInputState = Literal["ok", "no-data"]

# --- table kind limits (FR1) --- #
# A timer-driven table may not sample faster than this.
TABLE_MIN_GRID_DT_NS = 100_000_000
# Row count N: 1 <= N <= this (Fox's output limit); rejected outside, never clamped.
TABLE_MAX_POINTS = 10_000
# N when the spec gives none.
TABLE_DEFAULT_MAX_POINTS = 1_000
# Longest lookback a table subscribe may ask for (checked where t0 arrives).
TABLE_MAX_LOOKBACK_NS = 24 * 3600 * 1_000_000_000


class FormulaSpec(CanopyModel):
    """A complete formula request.

    Owl stores these in its plot configs; Fox accepts them as input.
    """

    kind: FormulaKind
    # Required for every kind but ``table``, whose expressions live in ``columns``.
    expression: str | None = None
    bindings: dict[str, str]
    mode: FormulaMode = "raw"
    align: AlignmentMode = "last"
    index: IndexMode = "union"
    # Both must be positive when provided; 0/negative previously flowed through
    # to a bare ValueError (grid) or a negative bucket count (max_points).
    grid_dt_ns: int | None = Field(default=None, gt=0)
    max_points: int | None = Field(default=None, gt=0)
    # ``table`` only: column name -> expression. A bare ``{binding}`` passes the
    # input through unchanged.
    columns: dict[str, str] | None = None
    # ``table`` with ``index='first_input'`` only: the binding whose updates
    # produce rows. The server indexes on this, never on binding order.
    trigger: str | None = None

    @model_validator(mode="after")
    def _check_kind_shape(self) -> "FormulaSpec":
        if self.kind != "table":
            if self.expression is None:
                raise ValueError(f"'expression' is required for kind '{self.kind}'")
            if self.columns is not None:
                raise ValueError("'columns' is only valid for kind 'table'")
            if self.trigger is not None:
                raise ValueError("'trigger' is only valid for kind 'table'")
            return self
        return self._check_table_shape()

    def _check_table_shape(self) -> "FormulaSpec":
        if self.expression is not None:
            raise ValueError("a table carries its expressions in 'columns', not 'expression'")
        if not self.columns:
            raise ValueError("a table needs at least one column")
        if any(not name.strip() for name in self.columns):
            raise ValueError("table column names must be non-empty")
        if self.mode != "raw":
            raise ValueError("a table is evaluated in 'raw' mode only")
        if self.align != "last":
            raise ValueError("a table aligns on the last value only ('align': 'last')")
        if self.index == "first_input":
            if self.trigger is None:
                raise ValueError("a table indexed on 'first_input' needs a 'trigger' binding")
            if self.trigger not in self.bindings:
                raise ValueError(f"trigger '{self.trigger}' is not a key of 'bindings'")
        elif self.index == "grid":
            if self.trigger is not None:
                raise ValueError("'trigger' is not allowed for a table indexed on 'grid'")
            if self.grid_dt_ns is None:
                raise ValueError("a table indexed on 'grid' needs 'gridDtNs'")
            if self.grid_dt_ns < TABLE_MIN_GRID_DT_NS:
                raise ValueError(f"a table's 'gridDtNs' must be at least {TABLE_MIN_GRID_DT_NS} (100 ms)")
        else:
            raise ValueError("a table is indexed on 'first_input' (trigger PV) or 'grid' (timer)")
        if self.max_points is None:
            self.max_points = TABLE_DEFAULT_MAX_POINTS
        elif not 1 <= self.max_points <= TABLE_MAX_POINTS:
            raise ValueError(f"a table's 'maxPoints' must be between 1 and {TABLE_MAX_POINTS}")
        return self


class ScalarResult(CanopyModel):
    ts_ns: int
    value: float | int | bool | str
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


class TableInput(CanopyModel):
    """Health of one table input (FR2)."""

    history: TableHistory
    status: TableInputState
    # True while this input delivered a sample late, or its IOC clock was seen to
    # skew, within the last 60 s of the server's receive clock. A state, not a count.
    late_after_final: bool


class TableResult(CanopyModel):
    """Rows of a live table: one shared index, one float (or null) per column."""

    ts_ns: list[int]
    columns: dict[str, list[float | None]]
    inputs: dict[str, TableInput]
    total_rows: int


class FormulaResult(CanopyModel):
    """Tagged union by `kind`. Frontend discriminates on this field."""

    kind: FormulaKind
    scalar: ScalarResult | None = None
    timeseries: TimeseriesResult | None = None
    waveform: WaveformResult | None = None
    table: TableResult | None = None


class FormulaValidationResult(CanopyModel):
    """Result of a parse+typecheck without evaluation."""

    valid: bool
    tokens: list[str] = []
    errors: list[str] = []
