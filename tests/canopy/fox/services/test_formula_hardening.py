"""Hardening: bad PV value types and out-of-range spec fields must produce
typed 4xx errors, never a 500."""

import pytest
from pydantic import ValidationError

from canopy.fox.config import FoxConfig
from canopy.fox.errors import FormulaTypeError
from canopy.contracts.sample import Sample, AlarmSeverity
from canopy.contracts.formula import FormulaSpec
from tests.canopy.fox.conftest import FakeDataSource
from canopy.fox.services.parser import Parser
from canopy.fox.services.scalar_evaluator import evaluate_scalar_formula
from canopy.fox.services.waveform_evaluator import evaluate_waveform_formula


async def test_scalar_formula_on_non_numeric_value_raises_type_error():
    # A non-numeric (here bytes) value bound to a scalar formula used to hit
    # float(value) -> TypeError -> 500. Now a typed FormulaTypeError (422).
    ds = FakeDataSource(
        latest={"PV:B": Sample(ts_ns=1, value=b"\x01\x02", severity=AlarmSeverity.NO_ALARM, source="live")}
    )
    spec = FormulaSpec(kind="scalar", expression="{a} + 1", bindings={"a": "PV:B"})
    parsed = Parser().parse(spec.expression, spec.bindings)
    with pytest.raises(FormulaTypeError):
        await evaluate_scalar_formula(spec, parsed, ds, FoxConfig())


async def test_waveform_formula_on_non_numeric_string_raises_type_error():
    ds = FakeDataSource(
        latest={"PV:S": Sample(ts_ns=1, value="not_a_number", severity=AlarmSeverity.NO_ALARM, source="live")}
    )
    spec = FormulaSpec(kind="waveform", expression="{a} * 2", bindings={"a": "PV:S"})
    parsed = Parser().parse(spec.expression, spec.bindings)
    with pytest.raises(FormulaTypeError):
        await evaluate_waveform_formula(spec, parsed, ds, FoxConfig())


@pytest.mark.parametrize("field,value", [("grid_dt_ns", 0), ("grid_dt_ns", -1), ("max_points", 0), ("max_points", -5)])
def test_formula_spec_rejects_nonpositive_numeric_fields(field, value):
    with pytest.raises(ValidationError):
        FormulaSpec(kind="timeseries", expression="{a}", bindings={"a": "PV:A"}, **{field: value})
