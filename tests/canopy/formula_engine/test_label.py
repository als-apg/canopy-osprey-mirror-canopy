"""``label({x})``: the enum string of a channel's value, in a scalar formula.

A discrete channel's value is an index; what it means is the channel's own enum
string. ``label({v}) == 'Open'`` compares on the meaning, so a rule does not
restate the index of every state. The shared case table
(``test_shared_vector_cases.py``) holds the semantics both evaluators agree on;
this file holds what only the server has: where the strings come from.
"""

import math
from dataclasses import field, dataclass

import pytest

from canopy.contracts.pv import PVMeta
from canopy.vole.adapter import VoleDataSource
from canopy.contracts.sample import Sample, AlarmSeverity
from canopy.contracts.formula import FormulaSpec
from canopy.formula_engine.config import FormulaEngineConfig
from canopy.formula_engine.errors import (
    FormulaParseError,
    FormulaInputOutage,
    FormulaInputMissing,
)
from canopy.vole.services.hare_ingest import HareIngestService
from tests.canopy.formula_engine.conftest import FakeDataSource
from canopy.formula_engine.services.parser import Parser
from canopy.formula_engine.services.formula_service import FormulaService

STATES = ["Closed", "Moving", "Open"]


@dataclass
class _Source(FakeDataSource):
    labels: dict[str, list[str]] = field(default_factory=dict)
    asked: list[str] = field(default_factory=list)

    async def enum_labels(self, pv: str) -> list[str] | None:
        self.asked.append(pv)
        return self.labels.get(pv)


def _valve(value, labels=STATES) -> _Source:
    sample = Sample(ts_ns=1000, value=value, severity=AlarmSeverity.NO_ALARM, source="live")
    return _Source(latest={"DEV:V": sample}, labels={"DEV:V": labels} if labels else {})


async def _value(source: _Source, expression: str, bindings: dict[str, str] | None = None):
    service = FormulaService(source, Parser(), FormulaEngineConfig())
    spec = FormulaSpec(kind="scalar", expression=expression, bindings=bindings or {"v": "DEV:V"})
    return (await service.evaluate(spec, t0_ns=0, t1_ns=0)).scalar


class TestTheString:
    async def test_the_result_can_be_the_string_itself(self):
        scalar = await _value(_valve(2), "label({v})")
        assert scalar.value == "Open"
        assert scalar.inputs_used == {"DEV:V": 1000}

    @pytest.mark.parametrize(
        ("value", "label"),
        [
            (0, "Closed"),
            (2.0, "Open"),  # a whole number written as a float is the same index
            (1.5, ""),
            (-1, ""),
            (3, ""),
            (math.nan, ""),
            (math.inf, ""),
            (True, ""),  # a flag is not an index
            (None, ""),
        ],
    )
    async def test_only_a_whole_number_inside_the_strings_has_a_label(self, value, label):
        assert (await _value(_valve(value), "label({v}) == '" + label + "'")).value is True

    async def test_the_strings_are_read_once_per_evaluation(self):
        source = _valve(1)
        await _value(source, "0 if label({v}) == 'Open' else (1 if label({v}) == 'Moving' else 2)")
        assert source.asked == ["DEV:V"]

    async def test_the_value_and_the_label_of_one_channel_together(self):
        assert (await _value(_valve(2), "{v} * 10 + (1 if label({v}) == 'Open' else 0)")).value == 21


class TestWithoutAValue:
    """It reads the value, so it fails as a plain token does; no string is asked for."""

    async def test_a_lost_connection_fails_the_formula(self):
        source = _valve(2)
        source.outages.add("DEV:V")
        with pytest.raises(FormulaInputOutage, match="DEV:V"):
            await _value(source, "label({v}) == 'Open'")
        assert source.asked == []

    async def test_no_sample_fails_the_formula(self):
        source = _Source(labels={"DEV:V": STATES})
        with pytest.raises(FormulaInputMissing, match="DEV:V"):
            await _value(source, "label({v}) == 'Open'")
        assert source.asked == []


class TestWhereItIsRefused:
    @pytest.mark.parametrize("expression", ["label({v} + 1)", "label({v}, 1)", "label(1) + {v}", "label() + {v}"])
    async def test_it_takes_exactly_one_token(self, expression):
        with pytest.raises(FormulaParseError, match=r"label\(\) takes exactly one token"):
            await _value(_valve(2), expression)

    async def test_not_a_vector_token(self):
        with pytest.raises(FormulaParseError, match="one channel"):
            await _value(_valve(2), "count_nonzero(label({g.*}) == 'Open')", {"g.0": "DEV:V"})

    @pytest.mark.parametrize("kind", ["timeseries", "waveform"])
    async def test_other_kinds(self, kind):
        service = FormulaService(_valve(2), Parser(), FormulaEngineConfig())
        spec = FormulaSpec(kind=kind, expression="label({v}) == 'Open'", bindings={"v": "DEV:V"})
        with pytest.raises(FormulaParseError, match="scalar"):
            await service.evaluate(spec, t0_ns=0, t1_ns=0)

    async def test_a_table_column(self):
        service = FormulaService(_valve(2), Parser(), FormulaEngineConfig())
        spec = FormulaSpec(
            kind="table",
            columns={"c": "label({v})", "v": "{v}"},
            bindings={"v": "DEV:V"},
            index="grid",
            grid_dt_ns=10**9,
        )
        assert not (await service.validate(spec)).valid


class _Broker:
    """A broker that holds metadata under the name a channel was subscribed with."""

    def __init__(self, choices: dict[str, list[str] | None]) -> None:
        self.choices = choices
        self.described: list[str] = []

    def add_client(self, client_id: str, channel) -> None:
        pass

    async def subscribe(self, client_id: str, pvs: list[str]) -> list[str]:
        return list(pvs)

    async def get_describe(self, pv: str) -> PVMeta:
        self.described.append(pv)
        if pv not in self.choices:
            raise RuntimeError("no such channel")
        return PVMeta(pv=pv, enum_choices=self.choices[pv])


class TestTheStringsOfTheLiveSide:
    """The broker reads a channel's metadata when it connects; the formula looks it up there."""

    async def _source(self, broker: _Broker, subscribed: list[str]) -> VoleDataSource:
        ingest = HareIngestService(buffer_service=None, broker=broker)
        await ingest.subscribe(subscribed)
        return VoleDataSource(buffer_service=None, enum_choices=ingest.enum_choices)

    async def test_a_subscribed_channel_gives_its_strings(self):
        source = await self._source(_Broker({"DEV:V": STATES}), ["DEV:V"])
        assert await source.enum_labels("DEV:V") == STATES

    async def test_the_protocol_prefix_of_a_binding_does_not_matter(self):
        broker = _Broker({"ca://DEV:V": STATES})
        source = await self._source(broker, ["ca://DEV:V"])
        assert await source.enum_labels("DEV:V") == STATES
        assert await source.enum_labels("ca://DEV:V") == STATES
        assert set(broker.described) == {"ca://DEV:V"}

    async def test_a_channel_without_strings_gives_none(self):
        source = await self._source(_Broker({"DEV:N": None, "DEV:E": []}), ["DEV:N", "DEV:E"])
        assert await source.enum_labels("DEV:N") is None
        assert await source.enum_labels("DEV:E") is None

    async def test_metadata_that_cannot_be_read_gives_none(self):
        source = await self._source(_Broker({}), ["DEV:V"])
        assert await source.enum_labels("DEV:V") is None

    async def test_a_data_source_without_a_live_side_gives_none(self):
        assert await VoleDataSource(buffer_service=None).enum_labels("DEV:V") is None
