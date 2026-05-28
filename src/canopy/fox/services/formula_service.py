"""FormulaService: orchestrates parse + evaluate by formula kind.

Lifecycle: one instance per ``canopy-api`` process. Constructed in
``canopy.fox.app.fox_lifespan``; injected into ``canopy.fox.gateway`` via the
module-level singleton there. Tests can re-bind freely.
"""

import logging

from canopy.fox.config import FoxConfig
from canopy.fox.ports import DataSource
from canopy.fox.errors import FormulaCircularReference
from canopy.fox.services.parser import Parser
from canopy.contracts.formula import FormulaSpec, FormulaResult, FormulaValidationResult
from canopy.fox.services.scalar_evaluator import evaluate_scalar_formula
from canopy.fox.services.curve_evaluator import evaluate_curve_formula
from canopy.fox.services.waveform_evaluator import evaluate_waveform_formula

logger = logging.getLogger(__name__)


class FormulaService:
    """Public business logic. Routes call this; gateway facades wrap this."""

    def __init__(self, data_source: DataSource, parser: Parser, config: FoxConfig) -> None:
        self._data_source = data_source
        self._parser = parser
        self._config = config

    async def evaluate(self, spec: FormulaSpec, *, t0_ns: int, t1_ns: int) -> FormulaResult:
        self._reject_fox_refs(spec)
        parsed = self._parser.parse(spec.expression, spec.bindings)

        match spec.kind:
            case "scalar":
                result = await evaluate_scalar_formula(spec, parsed, self._data_source, self._config)
                return FormulaResult(kind="scalar", scalar=result)
            case "curve":
                result = await evaluate_curve_formula(spec, parsed, self._data_source, t0_ns, t1_ns, self._config)
                return FormulaResult(kind="curve", curve=result)
            case "waveform":
                result = await evaluate_waveform_formula(spec, parsed, self._data_source, self._config)
                return FormulaResult(kind="waveform", waveform=result)

    async def validate(self, spec: FormulaSpec) -> FormulaValidationResult:
        """Parse + AST-check without touching the data source."""
        try:
            self._reject_fox_refs(spec)
            parsed = self._parser.parse(spec.expression, spec.bindings)
            return FormulaValidationResult(valid=True, tokens=parsed.tokens)
        except FormulaCircularReference as exc:
            return FormulaValidationResult(valid=False, errors=[str(exc)])
        except Exception as exc:
            return FormulaValidationResult(valid=False, errors=[str(exc)])

    def _reject_fox_refs(self, spec: FormulaSpec) -> None:
        """Bindings must resolve to PV names only — no formula-of-formula."""
        for var, pv in spec.bindings.items():
            if pv.startswith("fox:"):
                raise FormulaCircularReference(
                    f"Binding '{var}' references a derived formula '{pv}'; "
                    "formula-of-formula composition is not supported"
                )
