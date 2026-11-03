"""Unit tests for the Formula Engine validation seam (no HTTP, no real Formula Engine)."""

from canopy.contracts.formula import FormulaValidationResult
from canopy.converter.services.conversion_service import ConvertedFormula
from canopy.converter.adapters.formula_engine_validation import validate_formulas


def _formula(name="f-1", expr="a + b"):
    return ConvertedFormula(name=name, kind="scalar", expression=expr, bindings={"a": "PV:A", "b": "PV:B"})


async def test_valid_formulas_produce_no_warnings():
    async def ok(_spec):
        return FormulaValidationResult(valid=True)

    assert await validate_formulas([_formula(), _formula(name="f-2")], validate=ok) == []


async def test_invalid_formula_becomes_a_warning():
    async def bad(_spec):
        return FormulaValidationResult(valid=False, errors=["unknown token 'qux'"])

    warnings = await validate_formulas([_formula(name="f-x")], validate=bad)
    assert len(warnings) == 1
    assert "f-x" in warnings[0]
    assert "unknown token 'qux'" in warnings[0]


async def test_validator_exception_degrades_to_warning():
    async def boom(_spec):
        raise RuntimeError("formula engine down")

    warnings = await validate_formulas([_formula(name="f-y")], validate=boom)
    assert len(warnings) == 1
    assert "could not be validated" in warnings[0]


async def test_kind_expression_bindings_are_passed_through():
    seen = {}

    async def capture(spec):
        seen.update(kind=spec.kind, expression=spec.expression, bindings=spec.bindings)
        return FormulaValidationResult(valid=True)

    await validate_formulas([_formula(expr="x * 2")], validate=capture)
    assert seen == {"kind": "scalar", "expression": "x * 2", "bindings": {"a": "PV:A", "b": "PV:B"}}
