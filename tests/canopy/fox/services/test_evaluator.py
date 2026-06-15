"""Evaluator unit tests."""

import numpy as np
import pytest

from canopy.contracts.sample import AlarmSeverity
from canopy.fox.services.parser import Parser
from canopy.fox.services.evaluator import (
    evaluate_array,
    worst_severity,
    evaluate_scalar,
    worst_severity_arrays,
)


@pytest.fixture
def parser():
    return Parser()


class TestEvaluateScalar:
    async def test_simple_addition(self, parser):
        parsed = parser.parse("{a} + {b}", {"a": "PV:A", "b": "PV:B"})
        result = await evaluate_scalar(parsed, {"__v0": 5.0, "__v1": 3.0}, timeout_s=1.0)
        assert result == 8.0

    async def test_sqrt(self, parser):
        parsed = parser.parse("sqrt({x})", {"x": "PV:X"})
        result = await evaluate_scalar(parsed, {"__v0": 9.0}, timeout_s=1.0)
        assert result == 3.0

    async def test_boolean_result(self, parser):
        parsed = parser.parse("{a} > {b}", {"a": "PV:A", "b": "PV:B"})
        result = await evaluate_scalar(parsed, {"__v0": 10.0, "__v1": 5.0}, timeout_s=1.0)
        assert result is True


class TestEvaluateArray:
    async def test_numexpr_path(self, parser):
        parsed = parser.parse("{a} + {b}", {"a": "PV:A", "b": "PV:B"})
        assert parsed.is_numexpr_compatible
        a = np.array([1.0, 2.0, 3.0])
        b = np.array([4.0, 5.0, 6.0])
        result = await evaluate_array(parsed, {"__v0": a, "__v1": b}, timeout_s=1.0)
        np.testing.assert_array_equal(result, [5.0, 7.0, 9.0])

    async def test_asteval_fallback(self, parser):
        parsed = parser.parse("mean({a})", {"a": "PV:A"})
        assert not parsed.is_numexpr_compatible
        a = np.array([1.0, 2.0, 3.0])
        result = await evaluate_array(parsed, {"__v0": a}, timeout_s=1.0)
        assert float(result) == 2.0

    async def test_multiply(self, parser):
        parsed = parser.parse("{a} * {b}", {"a": "PV:A", "b": "PV:B"})
        a = np.array([1.0, 2.0, 3.0])
        b = np.array([4.0, 5.0, 6.0])
        result = await evaluate_array(parsed, {"__v0": a, "__v1": b}, timeout_s=1.0)
        np.testing.assert_array_equal(result, [4.0, 10.0, 18.0])


class TestSeverityPropagation:
    def test_worst_severity_basic(self):
        assert worst_severity([AlarmSeverity.NO_ALARM, AlarmSeverity.MINOR]) == AlarmSeverity.MINOR
        assert worst_severity([AlarmSeverity.MINOR, AlarmSeverity.MAJOR]) == AlarmSeverity.MAJOR
        assert worst_severity([AlarmSeverity.NO_ALARM]) == AlarmSeverity.NO_ALARM
        assert worst_severity([AlarmSeverity.INVALID, AlarmSeverity.NO_ALARM]) == AlarmSeverity.INVALID

    def test_worst_severity_arrays(self):
        a = np.array([0, 1, 0])  # NO_ALARM, MINOR, NO_ALARM
        b = np.array([0, 0, 2])  # NO_ALARM, NO_ALARM, MAJOR
        result = worst_severity_arrays([a, b])
        assert result == [AlarmSeverity.NO_ALARM, AlarmSeverity.MINOR, AlarmSeverity.MAJOR]
