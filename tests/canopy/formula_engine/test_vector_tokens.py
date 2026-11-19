"""Vector tokens and the status functions ``severity()`` and ``connected()``.

``{*}`` is the vector of every bound input, ``{g.*}`` the vector of the bindings
whose key starts with ``g.``. A reduction over a vector gives one value, so one
short scalar formula covers many channels.

A vector member, or a token read only through a status function, never fails
the formula: a member whose connection is down or that has no sample has value
NaN, severity 3 (INVALID) and ``connected`` 0, and makes the result's own
severity INVALID. A token whose value the expression reads directly keeps the
old rule: missing or in outage fails the whole formula.
"""

import math
import asyncio

import pytest

from canopy.core.errors import ErrorCode
from canopy.contracts.sample import Sample, AlarmSeverity
from canopy.contracts.formula import FormulaSpec
from canopy.formula_engine.config import FormulaEngineConfig
from canopy.formula_engine.errors import (
    InvalidSpec,
    FormulaTypeError,
    FormulaParseError,
    FormulaInputOutage,
    FormulaInputMissing,
)
from canopy.vole.services.notifier import ChangeNotifier
from tests.canopy.formula_engine.conftest import FakeDataSource
from canopy.formula_engine.services.parser import Parser
from canopy.formula_engine.services.formula_service import FormulaService
from canopy.formula_engine.services.subscription_manager import SubscriptionManager

SEV = {
    0: AlarmSeverity.NO_ALARM,
    1: AlarmSeverity.MINOR,
    2: AlarmSeverity.MAJOR,
    3: AlarmSeverity.INVALID,
}


def _sample(value, severity: int = 0, ts_ns: int = 1000) -> Sample:
    return Sample(ts_ns=ts_ns, value=value, severity=SEV[severity], source="live")


def _source(**samples: Sample) -> FakeDataSource:
    return FakeDataSource(latest={name.replace("_", ":"): s for name, s in samples.items()})


def _spec(expression: str, bindings: dict[str, str], kind: str = "scalar") -> FormulaSpec:
    return FormulaSpec(kind=kind, expression=expression, bindings=bindings)


def _service(source: FakeDataSource, **config) -> FormulaService:
    return FormulaService(source, Parser(), FormulaEngineConfig(**config))


async def _scalar(source: FakeDataSource, expression: str, bindings: dict[str, str], **config):
    result = await _service(source, **config).evaluate(_spec(expression, bindings), t0_ns=0, t1_ns=0)
    return result.scalar


THREE = {"0": "DEV:A", "1": "DEV:B", "2": "DEV:C"}


def _three(a=(1.0, 0), b=(2.0, 1), c=(4.0, 0)) -> FakeDataSource:
    return FakeDataSource(
        latest={
            "DEV:A": _sample(a[0], a[1], ts_ns=1000),
            "DEV:B": _sample(b[0], b[1], ts_ns=3000),
            "DEV:C": _sample(c[0], c[1], ts_ns=2000),
        }
    )


# --------------------------------------------------------------------------- #
# Parser
# --------------------------------------------------------------------------- #


class TestParser:
    def test_star_names_every_binding(self):
        parsed = Parser().parse("max({*})", THREE)
        assert parsed.vector_tokens == {"*": ("0", "1", "2")}

    def test_group_names_the_bindings_under_its_prefix(self):
        bindings = {"g.0": "DEV:A", "g.1": "DEV:B", "h.0": "DEV:C"}
        parsed = Parser().parse("max({g.*}) - max({h.*})", bindings)
        assert parsed.vector_tokens == {"g.*": ("g.0", "g.1"), "h.*": ("h.0",)}

    def test_group_members_do_not_depend_on_binding_order(self):
        forward = Parser().parse("sum({g.*})", {"g.a": "X", "g.b": "Y"})
        backward = Parser().parse("sum({g.*})", {"g.b": "Y", "g.a": "X"})
        assert forward.vector_tokens == backward.vector_tokens == {"g.*": ("g.a", "g.b")}

    def test_a_group_without_members_is_refused(self):
        with pytest.raises(FormulaParseError, match=r"\{g\.\*\} matches no binding"):
            Parser().parse("max({g.*}) + {a}", {"a": "DEV:A"})

    def test_a_binding_outside_every_group_is_still_unused(self):
        with pytest.raises(FormulaParseError, match="not used"):
            Parser().parse("max({g.*})", {"g.0": "DEV:A", "other": "DEV:B"})

    def test_star_uses_every_binding(self):
        Parser().parse("max({*}) + {a}", {"a": "DEV:A", "b": "DEV:B"})

    def test_a_binding_literally_named_like_a_vector_token_stays_scalar(self):
        parsed = Parser().parse("{x.*} + 1", {"x.*": "DEV:A"})
        assert parsed.vector_tokens == {}
        assert parsed.tokens == ["x.*"]

    def test_status_functions_take_one_token(self):
        parsed = Parser().parse("severity({a}) >= 2 || connected({a}) == 0", {"a": "DEV:A"})
        assert parsed.status_only == frozenset({"a"})

    def test_a_token_also_read_as_a_value_is_not_status_only(self):
        parsed = Parser().parse("severity({a}) + {a}", {"a": "DEV:A"})
        assert parsed.status_only == frozenset()

    @pytest.mark.parametrize(
        "expression",
        ["severity({a} + 1)", "severity(1) + {a}", "connected({a}, {a})", "severity() + {a}", "severity(max({a}))"],
    )
    def test_status_functions_refuse_anything_but_one_token(self, expression):
        with pytest.raises(FormulaParseError, match="exactly one"):
            Parser().parse(expression, {"a": "DEV:A"})

    def test_plain_formulas_parse_as_before(self):
        parsed = Parser().parse("{a} + {b} * 2", {"a": "X", "b": "Y"})
        assert parsed.placeholder_expr == "__v0 + __v1 * 2"
        assert parsed.tokens == ["a", "b"]
        assert parsed.vector_tokens == {}
        assert parsed.status_only == frozenset()
        assert not parsed.uses_status


# --------------------------------------------------------------------------- #
# Aggregates over connected members
# --------------------------------------------------------------------------- #


class TestAggregates:
    @pytest.mark.parametrize(
        ("expression", "expected"),
        [
            ("max({*})", 4.0),
            ("min({*})", 1.0),
            ("sum({*})", 7.0),
            ("mean({*})", 7.0 / 3.0),
            ("count({*})", 3),
            ("count_nonzero({*} == 2)", 1),
            ("count_nonzero({*} >= 2)", 2),
            ("max(severity({*}))", 1),
            ("count_nonzero(severity({*}) >= 1)", 1),
            ("sum(connected({*}))", 3),
            ("min(connected({*}))", 1),
            ("max({*}) - min({*})", 3.0),
        ],
    )
    async def test_reductions(self, expression, expected):
        result = await _scalar(_three(), expression, THREE)
        assert result.value == pytest.approx(expected)
        assert type(result.value) in (float, int, bool)

    async def test_result_carries_worst_member_severity_and_newest_timestamp(self):
        result = await _scalar(_three(), "max({*})", THREE)
        assert result.severity == AlarmSeverity.MINOR
        assert result.ts_ns == 3000

    async def test_members_are_counted_not_listed(self):
        """A thousand members would make every update a 30 kB frame."""
        result = await _scalar(_three(), "max({*})", THREE)
        assert result.inputs_used == {}
        assert result.vector_inputs.model_dump() == {"count": 3, "missing": [], "more": 0}

    async def test_a_plain_token_beside_a_vector_is_still_listed(self):
        bindings = {"g.0": "DEV:A", "g.1": "DEV:B", "limit": "DEV:C"}
        result = await _scalar(_three(), "count_nonzero({g.*} < {limit})", bindings)
        assert result.inputs_used == {"DEV:C": 2000}
        assert result.vector_inputs.count == 2

    async def test_a_status_function_on_one_channel_has_no_vector_summary(self):
        result = await _scalar(_three(), "severity({0}) + {1} + {2}", THREE)
        assert result.vector_inputs is None
        assert result.inputs_used == {"DEV:A": 1000, "DEV:B": 3000, "DEV:C": 2000}

    async def test_groups_reduce_separately(self):
        bindings = {"g.0": "DEV:A", "g.1": "DEV:B", "h.0": "DEV:C"}
        result = await _scalar(_three(), "sum({g.*}) * 10 + sum({h.*})", bindings)
        assert result.value == pytest.approx(34.0)

    async def test_vector_and_scalar_tokens_mix(self):
        bindings = {"g.0": "DEV:A", "g.1": "DEV:B", "limit": "DEV:C"}
        result = await _scalar(_three(), "count_nonzero({g.*} < {limit})", bindings)
        assert result.value == 2

    async def test_two_groups_pair_by_sorted_key(self):
        source = FakeDataSource(
            latest={name: _sample(v) for name, v in {"S:1": 10.0, "S:2": 20.0, "R:1": 9.0, "R:2": 23.0}.items()}
        )
        bindings = {"r.2": "R:2", "s.1": "S:1", "r.1": "R:1", "s.2": "S:2"}
        result = await _scalar(source, "max(abs({s.*} - {r.*}))", bindings)
        assert result.value == pytest.approx(3.0)

    async def test_an_unreduced_vector_is_a_type_error(self):
        with pytest.raises(FormulaTypeError, match="one value"):
            await _scalar(_three(), "{*} + 1", THREE)

    async def test_a_one_member_vector_still_needs_a_reduction(self):
        source = FakeDataSource(latest={"DEV:A": _sample(1.0)})
        with pytest.raises(FormulaTypeError, match="one value"):
            await _scalar(source, "{*} + 1", {"0": "DEV:A"})

    async def test_bool_and_integer_members_are_numbers(self):
        source = FakeDataSource(latest={"DEV:A": _sample(True), "DEV:B": _sample(2), "DEV:C": _sample(0)})
        result = await _scalar(source, "sum({*})", THREE)
        assert result.value == pytest.approx(3.0)


# --------------------------------------------------------------------------- #
# Members without a value
# --------------------------------------------------------------------------- #


class TestDisconnectedMembers:
    def _one_down(self) -> FakeDataSource:
        source = _three()
        source.outages.add("DEV:B")
        return source

    def _one_never_seen(self) -> FakeDataSource:
        source = _three()
        del source.latest["DEV:B"]
        return source

    @pytest.mark.parametrize("make", ["_one_down", "_one_never_seen"])
    @pytest.mark.parametrize(
        ("expression", "expected"),
        [
            ("max(severity({*}))", 3),
            ("count_nonzero(severity({*}) == 3)", 1),
            ("sum(connected({*}))", 2),
            ("min(connected({*}))", 0),
            ("max(connected({*}))", 1),
            ("count({*})", 3),
            ("nanmax({*})", 4.0),
            ("nanmin({*})", 1.0),
            ("nansum({*})", 5.0),
            ("count_nonzero({*} == 4)", 1),
        ],
    )
    async def test_a_member_without_a_value_does_not_fail_the_formula(self, make, expression, expected):
        result = await _scalar(getattr(self, make)(), expression, THREE)
        assert result.value == pytest.approx(expected)
        assert result.severity == AlarmSeverity.INVALID

    @pytest.mark.parametrize("expression", ["max({*})", "min({*})", "sum({*})", "mean({*})"])
    async def test_a_plain_reduction_names_the_member_in_outage(self, expression):
        """NaN is not JSON; the members without a value are reported instead."""
        with pytest.raises(FormulaInputOutage) as caught:
            await _scalar(self._one_down(), expression, THREE)
        assert caught.value.detail == "PVs not available: ['DEV:B']"

    async def test_a_plain_reduction_names_the_member_without_a_sample(self):
        with pytest.raises(FormulaInputMissing) as caught:
            await _scalar(self._one_never_seen(), "max({*})", THREE)
        assert not isinstance(caught.value, FormulaInputOutage)
        assert caught.value.detail == "PVs not available: ['DEV:B']"

    async def test_a_comparison_with_a_member_without_a_value_is_false_not_an_error(self):
        result = await _scalar(self._one_down(), "max({*}) > 3", THREE)
        assert result.value is False
        assert result.severity == AlarmSeverity.INVALID

    @pytest.mark.parametrize("value", [math.nan, math.inf])
    async def test_a_non_finite_result_of_connected_members_is_a_type_error(self, value):
        source = _three()
        source.latest["DEV:B"] = _sample(value)
        with pytest.raises(FormulaTypeError, match="not a finite number"):
            await _scalar(source, "max({*})", THREE)

    async def test_a_member_in_outage_is_not_read(self):
        """Its last sample is stale; it must not reach the value or the timestamps."""
        source = self._one_down()
        source.latest["DEV:B"] = _sample(1000.0, ts_ns=9000)
        result = await _scalar(source, "nansum({*})", THREE)
        assert result.value == pytest.approx(5.0)
        assert result.ts_ns == 2000

    @pytest.mark.parametrize("make", ["_one_down", "_one_never_seen"])
    async def test_the_members_without_a_value_are_named(self, make):
        result = await _scalar(getattr(self, make)(), "max(severity({*}))", THREE)
        assert result.vector_inputs.model_dump() == {"count": 3, "missing": ["DEV:B"], "more": 0}

    async def test_the_list_of_members_without_a_value_is_capped(self):
        bindings = {f"{i:02d}": f"DEV:{i:02d}" for i in range(30)}
        source = FakeDataSource(outages=set(bindings.values()))
        result = await _scalar(source, "sum(connected({*}))", bindings, vector_missing_listed=4)
        assert result.value == 0
        assert result.vector_inputs.model_dump() == {
            "count": 30,
            "missing": ["DEV:00", "DEV:01", "DEV:02", "DEV:03"],
            "more": 26,
        }

    async def test_the_error_of_a_value_reduction_is_capped_too(self):
        bindings = {f"{i:02d}": f"DEV:{i:02d}" for i in range(30)}
        source = FakeDataSource(outages=set(bindings.values()))
        with pytest.raises(FormulaInputOutage) as caught:
            await _scalar(source, "max({*})", bindings, vector_missing_listed=2)
        assert caught.value.detail == "PVs not available: ['DEV:00', 'DEV:01'] and 28 more"

    async def test_all_members_down_still_answers(self):
        source = _three()
        source.outages.update(THREE.values())
        result = await _scalar(source, "max(severity({*}))", THREE)
        assert result.value == 3
        assert result.severity == AlarmSeverity.INVALID
        assert result.inputs_used == {}
        assert result.vector_inputs.missing == ["DEV:A", "DEV:B", "DEV:C"]
        assert result.ts_ns > 0

    async def test_a_text_member_has_no_number(self):
        source = _three()
        source.latest["DEV:B"] = _sample("fault")
        result = await _scalar(source, "nansum({*}) + 100 * sum(connected({*}))", THREE)
        assert result.value == pytest.approx(305.0)


# --------------------------------------------------------------------------- #
# severity() and connected() on one channel
# --------------------------------------------------------------------------- #


class TestMembersThatAreText:
    async def test_text_that_reads_as_a_number_is_that_number(self):
        source = _three()
        source.latest["DEV:B"] = _sample("5")
        assert (await _scalar(source, "sum({*})", THREE)).value == 10.0

    async def test_other_text_has_no_number_but_is_connected(self):
        source = _three()
        source.latest["DEV:B"] = _sample("abc", severity=1)
        assert (await _scalar(source, "nansum({*})", THREE)).value == 5.0
        result = await _scalar(source, "sum(connected({*})) * 10 + max(severity({*}))", THREE)
        assert result.value == 31
        assert result.vector_inputs.missing == []


class TestStatusOfOneChannel:
    async def test_severity_and_connected_of_a_live_channel(self):
        source = FakeDataSource(latest={"DEV:A": _sample(7.0, severity=2)})
        result = await _scalar(source, "severity({a}) * 10 + connected({a})", {"a": "DEV:A"})
        assert result.value == 21
        assert result.severity == AlarmSeverity.MAJOR

    async def test_a_channel_read_only_for_status_does_not_fail_in_outage(self):
        source = FakeDataSource(latest={"DEV:A": _sample(7.0)}, outages={"DEV:A"})
        result = await _scalar(source, "severity({a}) * 10 + connected({a})", {"a": "DEV:A"})
        assert result.value == 30
        assert result.severity == AlarmSeverity.INVALID

    async def test_a_channel_read_only_for_status_does_not_fail_without_a_sample(self):
        result = await _scalar(FakeDataSource(), "connected({a})", {"a": "DEV:A"})
        assert result.value == 0
        assert result.severity == AlarmSeverity.INVALID

    async def test_a_value_read_keeps_the_outage_error(self):
        source = FakeDataSource(latest={"DEV:A": _sample(7.0)}, outages={"DEV:A"})
        with pytest.raises(FormulaInputOutage):
            await _scalar(source, "severity({a}) + {a}", {"a": "DEV:A"})

    async def test_a_value_read_keeps_the_missing_error(self):
        with pytest.raises(FormulaInputMissing):
            await _scalar(FakeDataSource(), "severity({a}) + {a}", {"a": "DEV:A"})

    async def test_a_value_token_beside_a_vector_keeps_the_outage_error(self):
        source = _three()
        source.outages.add("DEV:C")
        bindings = {"g.0": "DEV:A", "g.1": "DEV:B", "limit": "DEV:C"}
        with pytest.raises(FormulaInputOutage):
            await _scalar(source, "count_nonzero({g.*} < {limit})", bindings)

    async def test_plain_formulas_still_fail_on_a_missing_input(self):
        with pytest.raises(FormulaInputMissing):
            await _scalar(FakeDataSource(latest={"DEV:A": _sample(1.0)}), "{a} + {b}", {"a": "DEV:A", "b": "DEV:B"})


# --------------------------------------------------------------------------- #
# The shapes a screen viewer sends
# --------------------------------------------------------------------------- #


class TestRuleLadders:
    """A rule is one conditional ladder that answers the index of its first true arm."""

    LADDER = "0 if (severity({0}) >= 2) else (1 if (severity({0}) == 1) else (-1))"

    @pytest.mark.parametrize(("severity", "arm"), [(0, -1), (1, 1), (2, 0), (3, 0)])
    async def test_a_ladder_on_one_channels_severity(self, severity, arm):
        source = FakeDataSource(latest={"DEV:A": _sample(1.0, severity)})
        result = await _scalar(source, self.LADDER, {"0": "DEV:A"})
        assert result.value == arm

    async def test_a_ladder_on_a_disconnected_channel_takes_the_worst_arm(self):
        source = FakeDataSource(latest={"DEV:A": _sample(1.0)}, outages={"DEV:A"})
        result = await _scalar(source, self.LADDER, {"0": "DEV:A"})
        assert result.value == 0

    async def test_a_ladder_over_a_roll_up_inlined_under_its_formula_name(self):
        """An inlined formula's keys are prefixed with its name, so its ``{*}`` is ``{worst.*}``."""
        roll_up = "(max(severity({worst.*})))"
        ladder = f"0 if ({roll_up} >= 2) else (1 if ({roll_up} == 1 && {{2}} > 0) else (-1))"
        bindings = {"worst.0": "DEV:A", "worst.1": "DEV:B", "2": "DEV:C"}
        assert (await _scalar(_three(), ladder, bindings)).value == 1
        down = _three()
        down.outages.add("DEV:A")
        assert (await _scalar(down, ladder, bindings)).value == 0

    async def test_a_nested_group_is_inside_its_parent_group(self):
        bindings = {"f.g.0": "DEV:A", "f.g.1": "DEV:B", "f.h.0": "DEV:C"}
        result = await _scalar(_three(), "sum({f.g.*}) * 100 + sum({f.*})", bindings)
        assert result.value == pytest.approx(307.0)


# --------------------------------------------------------------------------- #
# Limits and kinds
# --------------------------------------------------------------------------- #


class TestLimits:
    def _many(self, n: int) -> tuple[FakeDataSource, dict[str, str]]:
        bindings = {str(i): f"DEV:{i}" for i in range(n)}
        return FakeDataSource(latest={pv: _sample(1.0) for pv in bindings.values()}), bindings

    async def test_a_vector_at_the_cap_evaluates(self):
        source, bindings = self._many(5)
        result = await _scalar(source, "sum({*})", bindings, max_vector_inputs=5)
        assert result.value == pytest.approx(5.0)

    async def test_a_vector_above_the_cap_is_refused_and_names_the_cap(self):
        source, bindings = self._many(6)
        with pytest.raises(InvalidSpec, match=r"at most 5 .*got 6.*MAX_VECTOR_INPUTS"):
            await _scalar(source, "sum({*})", bindings, max_vector_inputs=5)

    async def test_validate_reports_the_cap(self):
        source, bindings = self._many(6)
        result = await _service(source, max_vector_inputs=5).validate(_spec("sum({*})", bindings))
        assert not result.valid
        assert "at most 5" in result.errors[0]

    async def test_the_cap_does_not_apply_to_plain_formulas(self):
        source, bindings = self._many(3)
        result = await _scalar(source, "{0} + {1} + {2}", bindings, max_vector_inputs=1)
        assert result.value == pytest.approx(3.0)

    async def test_the_expression_stays_short_for_many_inputs(self):
        source, bindings = self._many(1000)
        result = await _scalar(source, "max(severity({*}))", bindings)
        assert result.value == 0

    @pytest.mark.parametrize("kind", ["timeseries", "waveform"])
    @pytest.mark.parametrize("expression", ["max({*})", "severity({0})", "connected({0}) + {0}"])
    async def test_other_kinds_refuse_vectors_and_status(self, kind, expression):
        service = _service(FakeDataSource())
        result = await service.validate(_spec(expression, {"0": "DEV:A"}, kind=kind))
        assert not result.valid
        assert "scalar" in result.errors[0]
        with pytest.raises(FormulaParseError, match="scalar"):
            await service.evaluate(_spec(expression, {"0": "DEV:A"}, kind=kind), t0_ns=0, t1_ns=0)

    @pytest.mark.parametrize("column", ["max({*})", "severity({a})", "connected({a}) + {a}"])
    async def test_a_table_column_refuses_vectors_and_status(self, column):
        spec = FormulaSpec(
            kind="table", columns={"c": column, "a": "{a}"}, bindings={"a": "DEV:A"}, index="grid", grid_dt_ns=10**9
        )
        result = await _service(FakeDataSource()).validate(spec)
        assert not result.valid

    async def test_validate_lists_the_tokens_as_written(self):
        result = await _service(FakeDataSource()).validate(_spec("max(severity({*}))", THREE))
        assert result.valid
        assert result.tokens == ["*"]


# --------------------------------------------------------------------------- #
# Live subscriptions
# --------------------------------------------------------------------------- #


class _Collector:
    def __init__(self) -> None:
        self.messages: list[dict] = []

    async def send(self, msg: dict) -> None:
        self.messages.append(msg)

    def values(self) -> list:
        return [m["data"]["scalar"]["value"] for m in self.messages if m["type"] == "result"]


class _Monotonic:
    """A clock the test moves: the manager's start window is read from it."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class _CountingService(FormulaService):
    evaluations = 0

    async def evaluate(self, spec, *, t0_ns, t1_ns):
        self.evaluations += 1
        return await super().evaluate(spec, t0_ns=t0_ns, t1_ns=t1_ns)


async def _wait_for(predicate, timeout: float = 2.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.005)


def _manager(source: FakeDataSource, notifier: ChangeNotifier, **kwargs):
    async def subscribe(pvs: list[str]) -> None:
        return None

    service = _CountingService(source, Parser(), FormulaEngineConfig())
    manager = SubscriptionManager(
        formula_service=service,
        on_change_fn=notifier.subscribe,
        subscribe_live_fn=subscribe,
        subscribe_latest_fn=subscribe,
        **kwargs,
    )
    return manager, service


class TestFirstResult:
    """No result goes out while a member's first value can still arrive.

    A member with neither a sample nor a known connection failure is pending.
    Until every member is settled, or the start window has passed, a formula
    with a vector token sends nothing, so a light does not show the
    disconnected state for the moment a screen takes to connect.
    """

    WINDOW_MS = 1000

    def _start(self, source, notifier, clock, **kwargs):
        return _manager(
            source, notifier, vector_interval_ms=10, vector_start_window_ms=self.WINDOW_MS, monotonic=clock, **kwargs
        )

    async def test_nothing_is_sent_while_a_member_is_pending(self):
        source, notifier, clock = _three(), ChangeNotifier(), _Monotonic()
        late = source.latest.pop("DEV:C")
        manager, service = self._start(source, notifier, clock)
        out = _Collector()
        try:
            await manager.add_subscription("c", "s", _spec("sum(connected({*}))", THREE), 0, 0, out.send)
            await _wait_for(lambda: service.evaluations >= 3)  # it keeps looking
            clock.now += 0.999
            await asyncio.sleep(0.1)
            assert out.messages == []
            source.latest["DEV:C"] = late
            notifier.notify("DEV:C")
            await _wait_for(lambda: out.values() == [3])
        finally:
            await manager.shutdown()

    async def test_a_complete_first_result_is_sent_at_once(self):
        source, notifier, clock = _three(), ChangeNotifier(), _Monotonic()
        manager, service = self._start(source, notifier, clock)
        out = _Collector()
        try:
            await manager.add_subscription("c", "s", _spec("sum(connected({*}))", THREE), 0, 0, out.send)
            await _wait_for(lambda: out.values() == [3])
            assert service.evaluations == 1
            assert clock.now == 0.0
        finally:
            await manager.shutdown()

    async def test_a_known_connection_failure_is_settled_not_pending(self):
        source, notifier, clock = _three(), ChangeNotifier(), _Monotonic()
        del source.latest["DEV:C"]
        source.outages.add("DEV:C")
        manager, _ = self._start(source, notifier, clock)
        out = _Collector()
        try:
            await manager.add_subscription("c", "s", _spec("sum(connected({*}))", THREE), 0, 0, out.send)
            await _wait_for(lambda: out.values() == [2])
            assert clock.now == 0.0
        finally:
            await manager.shutdown()

    async def test_the_failure_of_the_last_pending_member_releases_the_result(self):
        source, notifier, clock = _three(), ChangeNotifier(), _Monotonic()
        del source.latest["DEV:C"]
        manager, _ = self._start(source, notifier, clock)
        out = _Collector()
        try:
            await manager.add_subscription("c", "s", _spec("max(severity({*}))", THREE), 0, 0, out.send)
            await asyncio.sleep(0.1)
            assert out.messages == []
            source.outages.add("DEV:C")
            notifier.notify("DEV:C")
            await _wait_for(lambda: out.values() == [3])
        finally:
            await manager.shutdown()

    async def test_the_start_window_ends_the_wait(self):
        source, notifier, clock = _three(), ChangeNotifier(), _Monotonic()
        del source.latest["DEV:C"]
        manager, _ = self._start(source, notifier, clock)
        out = _Collector()
        try:
            await manager.add_subscription("c", "s", _spec("sum(connected({*}))", THREE), 0, 0, out.send)
            await asyncio.sleep(0.1)
            assert out.messages == []
            clock.now += 1.0
            await _wait_for(lambda: out.values() == [2])
            (frame,) = out.messages
            assert frame["data"]["scalar"]["vectorInputs"] == {"count": 3, "missing": ["DEV:C"], "more": 0}
        finally:
            await manager.shutdown()

    async def test_after_the_first_result_a_pending_member_holds_nothing(self):
        """A member that is added to the picture later (its ring evicted, say) does not silence the formula."""
        source, notifier, clock = _three(), ChangeNotifier(), _Monotonic()
        manager, _ = self._start(source, notifier, clock)
        out = _Collector()
        try:
            await manager.add_subscription("c", "s", _spec("sum(connected({*}))", THREE), 0, 0, out.send)
            await _wait_for(lambda: out.values() == [3])
            del source.latest["DEV:C"]
            notifier.notify("DEV:C")
            await _wait_for(lambda: out.values() == [3, 2])
            assert clock.now == 0.0
        finally:
            await manager.shutdown()

    async def test_a_window_of_zero_sends_the_first_result_as_it_is(self):
        source, notifier, clock = _three(), ChangeNotifier(), _Monotonic()
        del source.latest["DEV:C"]
        manager, _ = _manager(source, notifier, vector_interval_ms=10, vector_start_window_ms=0, monotonic=clock)
        out = _Collector()
        try:
            await manager.add_subscription("c", "s", _spec("sum(connected({*}))", THREE), 0, 0, out.send)
            await _wait_for(lambda: out.values() == [2])
        finally:
            await manager.shutdown()

    async def test_a_polled_roll_up_waits_too(self):
        source, notifier, clock = _three(), ChangeNotifier(), _Monotonic()
        late = source.latest.pop("DEV:C")
        manager, _ = self._start(source, notifier, clock)
        out = _Collector()
        try:
            await manager.add_subscription(
                "c", "s", _spec("sum(connected({*}))", THREE), 0, 0, out.send, interval_ms=5000
            )
            await asyncio.sleep(0.1)
            assert out.messages == []
            source.latest["DEV:C"] = late
            await _wait_for(lambda: out.values() == [3], timeout=1.0)
        finally:
            await manager.shutdown()

    async def test_a_late_joiner_of_a_waiting_roll_up_gets_the_first_result(self):
        source, notifier, clock = _three(), ChangeNotifier(), _Monotonic()
        late = source.latest.pop("DEV:C")
        manager, _ = self._start(source, notifier, clock)
        first, second = _Collector(), _Collector()
        spec = _spec("sum(connected({*}))", THREE)
        try:
            await manager.add_subscription("c1", "s", spec, 0, 0, first.send)
            await manager.add_subscription("c2", "s", spec, 0, 0, second.send)
            source.latest["DEV:C"] = late
            notifier.notify("DEV:C")
            await _wait_for(lambda: first.values() == [3] and second.values() == [3])
        finally:
            await manager.shutdown()

    async def test_a_status_formula_on_one_channel_does_not_wait(self):
        source, notifier, clock = FakeDataSource(), ChangeNotifier(), _Monotonic()
        manager, _ = self._start(source, notifier, clock)
        out = _Collector()
        try:
            await manager.add_subscription("c", "s", _spec("connected({a})", {"a": "DEV:A"}), 0, 0, out.send)
            await _wait_for(lambda: out.values() == [0])
        finally:
            await manager.shutdown()


class TestLive:
    async def test_an_outage_reaches_the_roll_up_as_a_result_not_an_error(self):
        source, notifier = _three(), ChangeNotifier()
        manager, _ = _manager(source, notifier, vector_interval_ms=10)
        out = _Collector()
        spec = _spec("max(severity({*}))", THREE)
        try:
            await manager.add_subscription("c", "s", spec, 0, 0, out.send)
            await _wait_for(lambda: out.values() == [1])
            source.outages.add("DEV:C")
            notifier.notify("DEV:C")
            await _wait_for(lambda: out.values() == [1, 3])
            source.outages.discard("DEV:C")
            notifier.notify("DEV:C")
            await _wait_for(lambda: out.values() == [1, 3, 1])
            assert [m["type"] for m in out.messages] == ["result"] * 3
        finally:
            await manager.shutdown()

    async def test_equal_roll_ups_share_one_evaluation(self):
        source, notifier = _three(), ChangeNotifier()
        manager, service = _manager(source, notifier, vector_interval_ms=10)
        first, second = _Collector(), _Collector()
        spec = _spec("max(severity({*}))", THREE)
        try:
            await manager.add_subscription("c1", "s", spec, 0, 0, first.send)
            await _wait_for(lambda: first.values() == [1])
            await manager.add_subscription("c2", "s", _spec("max(severity({*}))", dict(THREE)), 0, 0, second.send)
            await _wait_for(lambda: second.values() == [1])
            assert service.evaluations == 1
            assert len(manager._shared_evals) == 1
        finally:
            await manager.shutdown()

    async def test_the_same_bindings_in_another_order_share_one_evaluation(self):
        source, notifier = _three(), ChangeNotifier()
        manager, service = _manager(source, notifier, vector_interval_ms=10)
        first, second = _Collector(), _Collector()
        backward = dict(reversed(THREE.items()))
        assert list(backward) != list(THREE)
        try:
            await manager.add_subscription("c1", "s", _spec("max(severity({*}))", THREE), 0, 0, first.send)
            await _wait_for(lambda: first.values() == [1])
            await manager.add_subscription("c2", "s", _spec("max(severity({*}))", backward), 0, 0, second.send)
            await _wait_for(lambda: second.values() == [1])
            assert service.evaluations == 1
            assert len(manager._shared_evals) == 1
        finally:
            await manager.shutdown()

    async def test_plain_formulas_share_across_binding_order_too(self):
        source, notifier = _three(), ChangeNotifier()
        manager, service = _manager(source, notifier)
        first, second = _Collector(), _Collector()
        expression = "{0} + {1} * 10 + {2} * 100"
        try:
            await manager.add_subscription("c1", "s", _spec(expression, THREE), 0, 0, first.send)
            await _wait_for(lambda: first.values() == [421.0])
            await manager.add_subscription(
                "c2", "s", _spec(expression, dict(reversed(THREE.items()))), 0, 0, second.send
            )
            await _wait_for(lambda: second.values() == [421.0])
            assert service.evaluations == 1
        finally:
            await manager.shutdown()

    async def test_other_bindings_do_not_share(self):
        source, notifier = _three(), ChangeNotifier()
        manager, _ = _manager(source, notifier)
        out = _Collector()
        swapped = {"0": "DEV:B", "1": "DEV:A", "2": "DEV:C"}
        try:
            await manager.add_subscription("c1", "s", _spec("{0} + {1} * 10 + {2} * 100", THREE), 0, 0, out.send)
            await manager.add_subscription("c2", "s", _spec("{0} + {1} * 10 + {2} * 100", swapped), 0, 0, out.send)
            assert len(manager._shared_evals) == 2
        finally:
            await manager.shutdown()

    async def test_a_burst_of_member_changes_is_evaluated_once_per_interval(self):
        source, notifier = _three(), ChangeNotifier()
        manager, service = _manager(source, notifier, vector_interval_ms=200, vector_start_window_ms=0)
        out = _Collector()
        try:
            await manager.add_subscription("c", "s", _spec("sum({*})", THREE), 0, 0, out.send)
            await _wait_for(lambda: len(out.values()) == 1)
            for i in range(50):
                source.latest["DEV:A"] = _sample(float(i))
                notifier.notify("DEV:A")
                await asyncio.sleep(0)
            await _wait_for(lambda: len(out.values()) == 2)
            await asyncio.sleep(0.05)
            assert service.evaluations == 2
            assert out.values()[-1] == pytest.approx(49.0 + 2.0 + 4.0)
        finally:
            await manager.shutdown()

    async def test_changes_inside_the_start_window_are_followed_quickly(self):
        """Values still settle while a screen opens; the long interval starts after the window."""
        source, notifier, clock = _three(), ChangeNotifier(), _Monotonic()
        manager, _ = _manager(source, notifier, vector_interval_ms=5000, vector_start_window_ms=1000, monotonic=clock)
        out = _Collector()
        try:
            await manager.add_subscription("c", "s", _spec("sum({*})", THREE), 0, 0, out.send)
            await _wait_for(lambda: out.values() == [7.0])
            source.latest["DEV:A"] = _sample(11.0)
            notifier.notify("DEV:A")
            await _wait_for(lambda: out.values() == [7.0, 17.0], timeout=0.5)
        finally:
            await manager.shutdown()

    async def test_a_subscription_lives_on_after_an_error_that_is_not_retryable(self):
        """The error is the answer for now; the next change of a member is evaluated again."""
        source, notifier = _three(), ChangeNotifier()
        source.latest["DEV:B"] = _sample(math.inf)
        manager, _ = _manager(source, notifier, vector_interval_ms=10)
        out = _Collector()
        try:
            await manager.add_subscription("c", "s", _spec("max({*})", THREE), 0, 0, out.send)
            await _wait_for(lambda: len(out.messages) == 1)
            assert out.messages[0]["code"] == ErrorCode.FORMULA_TYPE_ERROR.value
            assert not out.messages[0].get("retryable")
            source.latest["DEV:B"] = _sample(2.0)
            notifier.notify("DEV:B")
            await _wait_for(lambda: out.values() == [4.0])
        finally:
            await manager.shutdown()

    async def test_a_plain_formula_is_not_slowed_down(self):
        source, notifier = _three(), ChangeNotifier()
        manager, service = _manager(source, notifier, vector_interval_ms=5000)
        out = _Collector()
        try:
            await manager.add_subscription("c", "s", _spec("{0} + {1} + {2}", THREE), 0, 0, out.send)
            await _wait_for(lambda: len(out.values()) == 1)
            source.latest["DEV:A"] = _sample(10.0)
            notifier.notify("DEV:A")
            await _wait_for(lambda: len(out.values()) == 2, timeout=1.0)
        finally:
            await manager.shutdown()

    async def test_a_polled_roll_up_is_floored_to_the_vector_interval(self):
        source, notifier = _three(), ChangeNotifier()
        manager, _ = _manager(source, notifier, vector_interval_ms=400)
        out = _Collector()
        try:
            await manager.add_subscription("c", "s", _spec("sum({*})", THREE), 0, 0, out.send, interval_ms=50)
            (shared,) = manager._shared_evals.values()
            assert shared.interval_ms == 400
        finally:
            await manager.shutdown()

    async def test_a_roll_up_above_the_cap_is_refused_before_any_channel_is_subscribed(self):
        bindings = {str(i): f"DEV:{i}" for i in range(4)}
        source, notifier = FakeDataSource(), ChangeNotifier()
        subscribed: list[str] = []
        pinned: list[str] = []

        async def subscribe(pvs: list[str]) -> None:
            subscribed.extend(pvs)

        manager = SubscriptionManager(
            formula_service=FormulaService(source, Parser(), FormulaEngineConfig(max_vector_inputs=3)),
            on_change_fn=notifier.subscribe,
            subscribe_live_fn=subscribe,
            subscribe_latest_fn=subscribe,
        )
        manager._pin = lambda pvs, holder: pinned.extend(pvs)
        out = _Collector()
        try:
            with pytest.raises(InvalidSpec, match="at most 3"):
                await manager.add_subscription("c", "s", _spec("sum({*})", bindings), 0, 0, out.send)
            assert subscribed == [] and pinned == []
            assert manager._shared_evals == {}
            assert InvalidSpec.error_code == ErrorCode.INVALID_SPEC
        finally:
            await manager.shutdown()


# --------------------------------------------------------------------------- #
# Against the ring store and its change notifier
# --------------------------------------------------------------------------- #


class TestWithTheRingStore:
    """The real buffer tells its listeners about an outage, so a roll-up follows it."""

    NOW = 1_000_000_000_000

    def _buffer(self, notifier: ChangeNotifier):
        from canopy.core.clock import FakeClock
        from canopy.vole.config import VoleConfig
        from canopy.vole.buffer.store import PVStore
        from canopy.vole.services.buffer_service import BufferService
        from canopy.vole.services.eviction_service import EvictionService
        from canopy.vole.services.alignment_service import AlignmentService

        config = VoleConfig(max_points=100, global_bytes_max=1_000_000)
        store = PVStore(config)
        clock = FakeClock(start_ns=self.NOW)
        return BufferService(
            store, AlignmentService(), EvictionService(store, config), clock, config, notifier=notifier
        )

    async def test_a_roll_up_follows_a_disconnect_and_a_reconnect(self):
        from canopy.vole.adapter import VoleDataSource

        notifier = ChangeNotifier()
        buffer = self._buffer(notifier)
        for i, pv in enumerate(THREE.values()):
            await buffer.append(pv, _sample(float(i), ts_ns=self.NOW + i))
        source = VoleDataSource(buffer, is_live_fed=lambda name: True)
        manager, _ = _manager(source, notifier, vector_interval_ms=10)
        out = _Collector()
        spec = _spec("max(severity({*})) * 10 + sum(connected({*}))", THREE)
        try:
            await manager.add_subscription("c", "s", spec, 0, 0, out.send)
            await _wait_for(lambda: out.values() == [3])
            buffer.open_outage("DEV:B", self.NOW + 10)
            await _wait_for(lambda: out.values() == [3, 32])
            buffer.close_outage("DEV:B", self.NOW + 20)
            await buffer.append("DEV:B", _sample(1.0, severity=1, ts_ns=self.NOW + 20))
            await _wait_for(lambda: out.values()[-1] == 13)
            assert all(m["type"] == "result" for m in out.messages)
        finally:
            await manager.shutdown()

    async def test_a_member_that_never_connected_is_not_asked_of_the_archive(self):
        from canopy.vole.adapter import VoleDataSource

        fetched: list[str] = []

        async def fetch_window(pv, t0_ns, t1_ns, **kwargs):
            fetched.append(pv)
            return []

        buffer = self._buffer(ChangeNotifier())
        await buffer.append("DEV:A", _sample(1.0, ts_ns=self.NOW))
        await buffer.append("DEV:C", _sample(1.0, ts_ns=self.NOW))
        buffer.open_outage("DEV:B", self.NOW)  # down before its first sample: no ring
        source = VoleDataSource(buffer, fetch_window=fetch_window)
        result = await _scalar(source, "sum(connected({*}))", THREE)
        assert result.value == 2
        assert fetched == []


# --------------------------------------------------------------------------- #
# Over the WebSocket
# --------------------------------------------------------------------------- #


@pytest.fixture
def ws(monkeypatch):
    from fastapi.testclient import TestClient

    from canopy.main import app as parent_app
    from canopy.formula_engine.app import app as formula_engine_app
    from canopy.formula_engine.dependencies import (
        get_formula_service,
        get_subscription_manager,
    )

    source, notifier = _three(), ChangeNotifier()
    manager, service = _manager(source, notifier, vector_interval_ms=10)
    formula_engine_app.dependency_overrides[get_formula_service] = lambda: service
    formula_engine_app.dependency_overrides[get_subscription_manager] = lambda: manager
    try:
        with TestClient(parent_app) as client:
            with client.websocket_connect("/formula-engine/v1/ws/formulas?api_key=test-read") as socket:
                yield socket, source
    finally:
        formula_engine_app.dependency_overrides.pop(get_formula_service, None)
        formula_engine_app.dependency_overrides.pop(get_subscription_manager, None)


def _subscribe(socket, expression: str, bindings: dict[str, str]) -> dict:
    socket.send_json({"type": "subscribe", "spec": {"kind": "scalar", "expression": expression, "bindings": bindings}})
    assert socket.receive_json()["type"] == "subscribed"
    return socket.receive_json()


class TestWebSocket:
    def test_a_roll_up_arrives_as_one_result_frame(self, ws):
        socket, source = ws
        source.outages.add("DEV:C")
        frame = _subscribe(socket, "max(severity({*}))", THREE)
        assert frame["type"] == "result"
        assert frame["data"]["scalar"] == {
            "tsNs": 3000,
            "value": 3,
            "severity": "INVALID",
            "inputsUsed": {},
            "vectorInputs": {"count": 3, "missing": ["DEV:C"], "more": 0},
        }

    def test_a_plain_formula_frame_has_the_keys_it_always_had(self, ws):
        socket, _ = ws
        frame = _subscribe(socket, "{0} + {1} + {2}", THREE)
        assert frame["data"]["scalar"] == {
            "tsNs": 3000,
            "value": 7.0,
            "severity": "MINOR",
            "inputsUsed": {"DEV:A": 1000, "DEV:B": 3000, "DEV:C": 2000},
        }

    def test_a_value_reduction_over_a_member_in_outage_is_a_retryable_error_frame(self, ws):
        """Never a ``NaN`` literal: that is not JSON, and a client drops the frame."""
        socket, source = ws
        source.outages.add("DEV:C")
        frame = _subscribe(socket, "max({*})", THREE)
        assert frame["type"] == "error"
        assert frame["code"] == ErrorCode.FORMULA_INPUT_MISSING.value
        assert frame["retryable"] is True
        assert frame["message"] == "PVs not available: ['DEV:C']"
