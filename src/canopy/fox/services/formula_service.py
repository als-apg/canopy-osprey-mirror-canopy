"""FormulaService: orchestrates parse + evaluate by formula kind.

Lifecycle: one instance per ``canopy-api`` process. Constructed in
``canopy.fox.app.fox_lifespan``; injected into ``canopy.fox.gateway`` via the
module-level singleton there. Tests can re-bind freely.
"""

import time
import logging

from canopy.fox.ports import DataSource
from canopy.fox.config import FoxConfig
from canopy.fox.errors import (
    FoxError,
    InvalidSpec,
    TableLiveOnly,
    FormulaParseError,
    FormulaCircularReference,
)
from canopy.contracts.formula import (
    TABLE_MAX_LOOKBACK_NS,
    FormulaSpec,
    FormulaResult,
    FormulaValidationResult,
)
from canopy.epics.pv_protocol import validate_pv_name
from canopy.fox.services.parser import Parser, ParsedFormula
from canopy.fox.services.evaluator import bare_binding
from canopy.fox.services.scalar_evaluator import evaluate_scalar_formula
from canopy.fox.services.waveform_evaluator import evaluate_waveform_formula
from canopy.fox.services.timeseries_evaluator import evaluate_timeseries_formula

logger = logging.getLogger(__name__)


class FormulaService:
    """Public business logic. Routes call this; gateway facades wrap this."""

    def __init__(self, data_source: DataSource, parser: Parser, config: FoxConfig) -> None:
        self._data_source = data_source
        self._parser = parser
        self._config = config

    @property
    def config(self) -> FoxConfig:
        return self._config

    def parse_table_columns(self, spec: FormulaSpec) -> dict[str, ParsedFormula | None]:
        """Parsed expression per column of a validated table; None for a bare ``{binding}`` column."""
        parsed: dict[str, ParsedFormula | None] = {}
        for name, expression in (spec.columns or {}).items():
            if bare_binding(expression, spec.bindings) is not None:
                parsed[name] = None
                continue
            parsed[name] = self._parse_column(expression, self._parser.extract_tokens(expression), spec.bindings)
        return parsed

    def _parse_column(self, expression: str, tokens: list[str], bindings: dict[str, str]) -> ParsedFormula:
        """Parse one column expression against the bindings it references (its ``tokens``)."""
        return self._parser.parse(expression, {t: bindings[t] for t in tokens if t in bindings})

    async def evaluate(self, spec: FormulaSpec, *, t0_ns: int, t1_ns: int) -> FormulaResult:
        # One guard covers REST evaluate, the WS one-shot and the in-process
        # gateway; the live table loop does not come through here.
        if spec.kind == "table":
            raise TableLiveOnly(
                "a table formula is live-only: subscribe over the WebSocket with t1Ns=0 and a negative t0Ns"
            )
        if t1_ns == 0:
            t1_ns = time.time_ns()
        if t0_ns < 0:
            t0_ns = t1_ns + t0_ns

        self._validate_bindings(spec)
        parsed = self._parser.parse(spec.expression, spec.bindings)

        match spec.kind:
            case "scalar":
                result = await evaluate_scalar_formula(spec, parsed, self._data_source, self._config)
                return FormulaResult(kind="scalar", scalar=result)
            case "timeseries":
                result = await evaluate_timeseries_formula(spec, parsed, self._data_source, t0_ns, t1_ns, self._config)
                return FormulaResult(kind="timeseries", timeseries=result)
            case "waveform":
                result = await evaluate_waveform_formula(spec, parsed, self._data_source, self._config)
                return FormulaResult(kind="waveform", waveform=result)

    async def validate(self, spec: FormulaSpec) -> FormulaValidationResult:
        """Parse + AST-check without touching the data source."""
        try:
            if spec.kind == "table":
                return FormulaValidationResult(valid=True, tokens=self.check_table(spec))
            self._validate_bindings(spec)
            parsed = self._parser.parse(spec.expression, spec.bindings)
            return FormulaValidationResult(valid=True, tokens=parsed.tokens)
        except FormulaCircularReference as exc:
            return FormulaValidationResult(valid=False, errors=[str(exc)])
        except Exception as exc:
            return FormulaValidationResult(valid=False, errors=[str(exc)])

    def _validate_bindings(self, spec: FormulaSpec) -> None:
        """Bindings must resolve to well-formed PV names only.

        Rejects formula-of-formula composition (``fox:`` refs) and malformed PV
        names (empty, over-long, protocol-prefixed garbage) so a bad binding
        can't reach the data source / EPICS layer.
        """
        for var, pv in spec.bindings.items():
            if pv.startswith("fox:"):
                raise FormulaCircularReference(
                    f"Binding '{var}' references a derived formula '{pv}'; "
                    "formula-of-formula composition is not supported"
                )
            validate_pv_name(pv)

    def check_table(self, spec: FormulaSpec) -> list[str]:
        """Validate a table synchronously, before any PV is pinned or subscribed.

        Caps, bindings and every non-bare column are checked here so that a
        malformed spec (say, from a URL query) never reaches Vole or Hare.
        Returns the binding names the columns use, in first-use order.

        Raises ``InvalidSpec`` over the column/binding caps, the binding errors
        of :meth:`_validate_bindings`, ``FormulaCircularReference`` for a column
        that references a column, and the parser's errors (prefixed with the
        column name) for a bad expression.
        """
        if spec.kind != "table" or spec.columns is None:
            raise ValueError("check_table() takes a table spec")
        max_columns = self._config.table_max_columns
        if len(spec.columns) > max_columns:
            raise InvalidSpec(f"a table has at most {max_columns} columns (got {len(spec.columns)})")
        if len(spec.bindings) > max_columns + 1:
            raise InvalidSpec(f"a table has at most {max_columns + 1} bindings (got {len(spec.bindings)})")
        self._validate_bindings(spec)

        used: list[str] = []
        for name, expression in spec.columns.items():
            tokens = self._parser.extract_tokens(expression)
            for token in tokens:
                if token not in spec.bindings and token in spec.columns:
                    raise FormulaCircularReference(
                        f"column '{name}' references column '{token}'; columns may only reference bindings"
                    )
            if bare_binding(expression, spec.bindings) is None:
                try:
                    self._parse_column(expression, tokens, spec.bindings)
                except FoxError as exc:
                    raise type(exc)(f"column '{name}': {exc.detail}") from None
            used.extend(t for t in tokens if t in spec.bindings)

        unused = set(spec.bindings) - set(used) - {spec.trigger}
        if unused:
            raise FormulaParseError(f"Bindings provided but not used by any column: {sorted(unused)}")
        return list(dict.fromkeys(used))

    @staticmethod
    def check_table_window(t0_ns: int) -> None:
        """A live table's ``t0Ns`` is a relative lookback: negative, at most 24 h."""
        if t0_ns >= 0:
            raise InvalidSpec("a table subscribe needs a relative lookback: t0Ns < 0")
        if -t0_ns > TABLE_MAX_LOOKBACK_NS:
            raise InvalidSpec(f"a table's lookback is at most 24 h (t0Ns >= -{TABLE_MAX_LOOKBACK_NS})")
