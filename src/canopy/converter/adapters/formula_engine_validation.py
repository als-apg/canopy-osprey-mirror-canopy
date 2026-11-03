"""Seam #2: validate hoisted ``calc://`` formulas via the Formula Engine at convert time.

The converter lowers ``calc://`` channel references to screen-level
``FormulaDeclaration``s but does not validate the expressions. Here each is
checked against the Formula Engine's grammar through its in-process gateway (parse +
AST-check, no data-source I/O — cheap). A failure becomes a convert-time
*warning*, never a hard error: an unviewable formula must not block import.
"""

from __future__ import annotations

from collections.abc import Callable, Awaitable

from canopy.contracts.formula import FormulaSpec, FormulaValidationResult
from canopy.converter.services.conversion_service import ConvertedFormula

# A Formula Engine validator: parse + typecheck a spec without touching the data source.
# ``canopy.formula_engine.gateway.validate`` satisfies this; tests inject a fake.
FormulaEngineValidate = Callable[[FormulaSpec], Awaitable[FormulaValidationResult]]


async def validate_formulas(formulas: list[ConvertedFormula], *, validate: FormulaEngineValidate) -> list[str]:
    """Validate each hoisted formula via the Formula Engine; return one warning per failure."""
    warnings: list[str] = []
    for formula in formulas:
        spec = FormulaSpec(kind=formula.kind, expression=formula.expression, bindings=formula.bindings)
        try:
            result = await validate(spec)
        except Exception as exc:  # noqa: BLE001 - Formula Engine unavailable/unexpected: degrade, never block import
            warnings.append(f"Formula '{formula.name}' could not be validated: {exc}")
            continue
        if not result.valid:
            detail = "; ".join(result.errors) or "invalid expression"
            warnings.append(f"Formula '{formula.name}' failed Formula Engine validation: {detail}")
    return warnings
