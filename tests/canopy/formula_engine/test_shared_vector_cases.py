"""The shared case table of formulas over many channels, evaluated on the server.

``fixtures/formula-vector-cases.json`` is one table for every evaluator of the
formula language: this server, and a client that evaluates formulas in the
page. Each side keeps a byte copy and evaluates every case, so the two cannot
drift apart. Change the table on both sides together, never the expectations
here alone. Its ``about`` field explains the format.
"""

import json
import math
from pathlib import Path
from dataclasses import field, dataclass

import pytest

from canopy.core.errors import ErrorCode, CanopyError
from canopy.contracts.sample import Sample, AlarmSeverity
from canopy.contracts.formula import FormulaSpec
from canopy.formula_engine.config import FormulaEngineConfig
from tests.canopy.formula_engine.conftest import FakeDataSource
from canopy.formula_engine.services.parser import Parser
from canopy.formula_engine.services.formula_service import FormulaService

TABLE = json.loads((Path(__file__).parent / "fixtures" / "formula-vector-cases.json").read_text(encoding="utf-8"))
CASES = TABLE["cases"]

SEVERITIES = [AlarmSeverity.NO_ALARM, AlarmSeverity.MINOR, AlarmSeverity.MAJOR, AlarmSeverity.INVALID]
NON_FINITE = {"nan": math.nan, "inf": math.inf, "-inf": -math.inf}
ERROR_CLASS = {
    ErrorCode.FORMULA_INPUT_MISSING: "input_missing",
    ErrorCode.FORMULA_TYPE_ERROR: "type_error",
    ErrorCode.FORMULA_PARSE_ERROR: "parse_error",
}


@dataclass
class _Channels(FakeDataSource):
    """The channels of one case: samples, lost connections and enum strings."""

    labels: dict[str, list[str]] = field(default_factory=dict)

    async def enum_labels(self, pv: str) -> list[str] | None:
        return self.labels.get(pv)


def _source(channels: dict[str, dict]) -> _Channels:
    source = _Channels()
    for name, channel in channels.items():
        if channel.get("down"):
            source.outages.add(name)
        if "severity" in channel:
            # A lost connection may keep its stale sample in the store; it must not be read.
            value = NON_FINITE[channel["nonFinite"]] if "nonFinite" in channel else channel.get("value")
            severity = SEVERITIES[channel["severity"]]
            source.latest[name] = Sample(ts_ns=1000, value=value, severity=severity, source="live")
        if "labels" in channel:
            source.labels[name] = channel["labels"]
    return source


def test_the_table_is_the_one_this_test_was_written_for():
    assert len(CASES) == 94
    assert len({case["name"] for case in CASES}) == len(CASES)


@pytest.mark.parametrize("case", CASES, ids=[f"{i:02d} {case['name']}" for i, case in enumerate(CASES, 1)])
async def test_the_server_evaluates_the_shared_case(case):
    config = FormulaEngineConfig(vector_missing_listed=case.get("missingListed", 20))
    service = FormulaService(_source(case["channels"]), Parser(), config)
    spec_fields = {"kind": "scalar", "expression": case["expression"], "bindings": case["bindings"]}

    if "error" in case:
        with pytest.raises(CanopyError) as raised:
            await service.evaluate(FormulaSpec(**spec_fields), t0_ns=0, t1_ns=0)
        assert ERROR_CLASS.get(raised.value.error_code) == case["error"], raised.value.detail
        if "detail" in case:
            assert raised.value.detail == case["detail"]
        return

    result = await service.evaluate(FormulaSpec(**spec_fields), t0_ns=0, t1_ns=0)
    scalar = result.model_dump(by_alias=True, mode="json")["scalar"]
    assert scalar["value"] == case["value"]
    assert type(scalar["value"]) is type(case["value"]) or {type(scalar["value"]), type(case["value"])} == {int, float}
    assert scalar["severity"] == case["severity"]
    assert scalar.get("vectorInputs") == case["vectorInputs"]
