"""Every input a formula cannot read is reported in one wording.

A client retries ``formula_input_missing`` whether the input has no sample yet
or its connection is down right now, and logs the detail as it stands; one
sentence for the one code keeps those logs readable as one state.
"""

import pytest

from canopy.fox.config import FoxConfig
from canopy.fox.errors import FormulaInputOutage, FormulaInputMissing
from canopy.contracts.sample import Sample, AlarmSeverity
from canopy.contracts.formula import FormulaSpec
from tests.canopy.fox.conftest import FakeDataSource
from canopy.fox.services.parser import Parser
from canopy.fox.services.scalar_evaluator import evaluate_scalar_formula
from canopy.fox.services.waveform_evaluator import evaluate_waveform_formula


def _sample(value) -> Sample:
    return Sample(ts_ns=1000, value=value, severity=AlarmSeverity.NO_ALARM, source="live")


SCALAR = FormulaSpec(kind="scalar", expression="{a} + {b}", bindings={"a": "PV:A", "b": "PV:B"})
WAVEFORM = FormulaSpec(kind="waveform", expression="{w} * 2", bindings={"w": "PV:WF"})


class TestForPvs:
    def test_names_every_input_in_one_wording(self):
        exc = FormulaInputMissing.for_pvs(["PV:A", "PV:B"])
        assert exc.detail == "PVs not available: ['PV:A', 'PV:B']"

    def test_an_outage_reads_the_same_and_keeps_its_class(self):
        exc = FormulaInputOutage.for_pvs(["PV:A"])
        assert exc.detail == "PVs not available: ['PV:A']"
        assert isinstance(exc, FormulaInputOutage)
        assert isinstance(exc, FormulaInputMissing)

    def test_accepts_any_iterable_in_its_order(self):
        assert FormulaInputMissing.for_pvs(dict.fromkeys(["PV:B", "PV:A"])).detail == (
            "PVs not available: ['PV:B', 'PV:A']"
        )


class TestEvaluators:
    async def test_scalar_input_without_a_sample(self):
        source = FakeDataSource(latest={"PV:A": _sample(5.0)})
        with pytest.raises(FormulaInputMissing) as info:
            await evaluate_scalar_formula(
                SCALAR, Parser().parse(SCALAR.expression, SCALAR.bindings), source, FoxConfig()
            )
        assert info.value.detail == "PVs not available: ['PV:B']"

    async def test_scalar_input_in_outage_reads_the_same(self):
        source = FakeDataSource(latest={"PV:A": _sample(5.0), "PV:B": _sample(3.0)}, outages={"PV:B"})
        with pytest.raises(FormulaInputOutage) as info:
            await evaluate_scalar_formula(
                SCALAR, Parser().parse(SCALAR.expression, SCALAR.bindings), source, FoxConfig()
            )
        assert info.value.detail == "PVs not available: ['PV:B']"

    async def test_waveform_input_without_a_sample(self):
        source = FakeDataSource()
        with pytest.raises(FormulaInputMissing) as info:
            await evaluate_waveform_formula(
                WAVEFORM, Parser().parse(WAVEFORM.expression, WAVEFORM.bindings), source, FoxConfig()
            )
        assert info.value.detail == "PVs not available: ['PV:WF']"
