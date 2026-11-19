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
            ("nansum(abs({*}) * 2) / count({*})", 10 / 3),
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

    @pytest.mark.parametrize("make", ["_one_down", "_one_never_seen"])
    @pytest.mark.parametrize(
        "expression",
        [
            "count_nonzero({*})",  # NaN is not zero: the member would be counted
            "count_nonzero({*} == 4)",
            "sum({*} != 0)",
            "max({*}) > 3",  # a comparison with NaN is false: it would read as "not above"
            "sum(where({*} > 1, 1, 0))",
            "nansum(where({*} > 1, 1, 0))",  # the comparison comes before the guard
            "nanmax({*} ** 0)",  # NaN ** 0 is 1
            "nanmax(clip({*}, 0, 2))",
            "1 if nanmax({*}) > 3 and min({*}) > 0 else 0",  # one use unguarded is enough
            "nanmax({*}, 0)",  # not the plain one-argument call
        ],
    )
    async def test_a_member_without_a_value_never_passes_for_a_number(self, make, expression):
        with pytest.raises(FormulaInputMissing) as caught:
            await _scalar(getattr(self, make)(), expression, THREE)
        assert caught.value.detail == "PVs not available: ['DEV:B']"
        assert isinstance(caught.value, FormulaInputOutage) is (make == "_one_down")

    async def test_the_same_expressions_answer_when_every_member_has_a_value(self):
        assert (await _scalar(_three(), "count_nonzero({*} == 4)", THREE)).value == 1
        assert (await _scalar(_three(), "max({*}) > 3", THREE)).value is True

    async def test_only_the_members_of_the_unguarded_token_count(self):
        """A member down in a group that is only rolled up by status does not fail the other group's values."""
        source = _three()
        source.outages.add("DEV:A")
        bindings = {"s.0": "DEV:A", "v.0": "DEV:B", "v.1": "DEV:C"}
        result = await _scalar(source, "max(severity({s.*})) * 100 + count_nonzero({v.*} > 1)", bindings)
        assert result.value == 302

    async def test_the_names_are_capped(self):
        source = FakeDataSource()
        bindings = {f"{i:02d}": f"DEV:{i:02d}" for i in range(30)}
        with pytest.raises(FormulaInputMissing) as caught:
            await _scalar(source, "count_nonzero({*})", bindings, vector_missing_listed=2)
        assert caught.value.detail == "PVs not available: ['DEV:00', 'DEV:01'] and 28 more"

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


class TestMembersWhoseValueIsNotFinite:
    """Connected, but the value itself is NaN or infinite: the cause is live data, not the formula."""

    @pytest.mark.parametrize(
        ("func", "bad"),
        [(f, math.nan) for f in ("max", "min", "sum", "mean")]
        + [("max", math.inf), ("sum", math.inf), ("min", -math.inf), ("mean", -math.inf)],
    )
    async def test_a_plain_reduction_names_the_member_and_is_retryable(self, func, bad):
        source = _three()
        source.latest["DEV:B"] = _sample(bad)
        with pytest.raises(FormulaInputMissing) as raised:
            await _scalar(source, f"{func}({{*}})", THREE)
        assert raised.value.detail == "PVs without a number: ['DEV:B']"
        assert raised.value.error_code is ErrorCode.FORMULA_INPUT_MISSING

    @pytest.mark.parametrize(("func", "value"), [("nanmax", 4.0), ("nanmin", 1.0), ("nansum", 5.0), ("nanmean", 2.5)])
    async def test_a_nan_reduction_skips_a_nan(self, func, value):
        source = _three()
        source.latest["DEV:B"] = _sample(math.nan, severity=2)
        result = await _scalar(source, f"{func}({{*}})", THREE)
        assert result.value == value
        assert result.severity is AlarmSeverity.MAJOR  # the member's own severity still counts
        assert result.vector_inputs.missing == []  # it is connected

    async def test_a_result_that_is_finite_all_the_same_is_given(self):
        source = _three()
        source.latest["DEV:B"] = _sample(-math.inf)
        assert (await _scalar(source, "max({*})", THREE)).value == 4.0

    async def test_its_status_is_read_as_sampled(self):
        source = _three()
        source.latest["DEV:B"] = _sample(math.nan, severity=1)
        assert (await _scalar(source, "sum(connected({*})) * 10 + max(severity({*}))", THREE)).value == 31

    async def test_members_that_are_not_there_are_named_first_and_alone(self):
        source = _three()
        source.latest["DEV:B"] = _sample(math.nan)
        del source.latest["DEV:C"]
        with pytest.raises(FormulaInputMissing) as raised:
            await _scalar(source, "max({*})", THREE)
        assert raised.value.detail == "PVs not available: ['DEV:C']"

    async def test_the_names_are_capped_in_the_same_way(self):
        bindings = {f"{i:02d}": f"DEV:{i:02d}" for i in range(30)}
        source = _source(**{pv: _sample(math.nan) for pv in bindings.values()})
        with pytest.raises(FormulaInputMissing) as raised:
            await _scalar(source, "max({*})", bindings, vector_missing_listed=2)
        assert raised.value.detail == "PVs without a number: ['DEV:00', 'DEV:01'] and 28 more"

    async def test_a_plain_token_beside_a_vector(self):
        source = _three()
        source.latest["DEV:C"] = _sample(math.nan)
        bindings = {"g.0": "DEV:A", "g.1": "DEV:B", "limit": "DEV:C"}
        with pytest.raises(FormulaInputMissing, match="DEV:C"):
            await _scalar(source, "sum({g.*}) + {limit}", bindings)

    async def test_a_non_finite_result_of_finite_inputs_stays_a_type_error(self):
        source = _three(a=(0.0, 0))
        with pytest.raises(FormulaTypeError, match="not a finite number"):
            await _scalar(source, "log(min({*}))", THREE)


class TestMembersThatAreNoNumber:
    """Text (also text that looks like a number), an array or an empty sample: connected, but no number."""

    @pytest.mark.parametrize("value", ["5", "abc", b"\x01\x02", None])
    async def test_it_is_skipped_or_named_like_a_member_without_a_value(self, value):
        source = _three()
        source.latest["DEV:B"] = _sample(value, severity=1)
        assert (await _scalar(source, "nansum({*})", THREE)).value == 5.0
        with pytest.raises(FormulaInputMissing, match=r"PVs without a number: \['DEV:B'\]"):
            await _scalar(source, "sum({*})", THREE)
        with pytest.raises(FormulaInputMissing, match="DEV:B"):
            await _scalar(source, "count_nonzero({*} == 5)", THREE)

    async def test_it_stays_connected_with_its_own_severity(self):
        source = _three()
        source.latest["DEV:B"] = _sample("5", severity=1)
        result = await _scalar(source, "sum(connected({*})) * 10 + max(severity({*}))", THREE)
        assert result.value == 31
        assert result.vector_inputs.missing == []

    async def test_a_plain_token_keeps_its_text(self):
        source = _source(**{"DEV:A": _sample("on")})
        assert (await _scalar(source, "severity({a}) + (1 if {a} == 'on' else 0)", {"a": "DEV:A"})).value == 1

    async def test_an_empty_sample_of_a_plain_token_is_no_zero(self):
        source = _source(**{"DEV:A": _sample(None)})
        with pytest.raises(FormulaInputMissing, match="DEV:A"):
            await _scalar(source, "severity({a}) + {a}", {"a": "DEV:A"})
        assert (await _scalar(source, "connected({a})", {"a": "DEV:A"})).value == 1


class TestStringLiterals:
    """The text of a string literal is never rewritten."""

    @pytest.mark.parametrize(
        ("expression", "text"),
        [
            ("{a} == 'Fault!'", "Fault!"),
            ("{a} == 'A&&B'", "A&&B"),
            ("{a} == 'a || b'", "a || b"),
            ('{a} == "it\'s !ok"', "it's !ok"),
            ("{a} == 'say \\'no!\\''", "say 'no!'"),
            ("{a} == '{b}'", "{b}"),  # not a token: needs no binding
            ("{a} == 'severity({a})'", "severity({a})"),
            ("{a} == 'a{b{c'", "a{b{c"),
            ("{a} == '''x && 'y' !'''", "x && 'y' !"),
        ],
    )
    async def test_a_literal_reaches_the_comparison_as_written(self, expression, text):
        source = _source(**{"DEV:A": _sample(text)})
        assert (await _scalar(source, f"severity({{a}}) + (1 if {expression} else 0)", {"a": "DEV:A"})).value == 1
        # the plain path too
        service = _service(source)
        plain = await service.evaluate(_spec(expression, {"a": "DEV:A"}), t0_ns=0, t1_ns=0)
        assert plain.scalar.value is True

    async def test_the_code_around_a_literal_is_still_rewritten(self):
        source = _source(**{"DEV:A": _sample("A&&B"), "DEV:B": _sample(1.0)})
        bindings = {"a": "DEV:A", "b": "DEV:B"}
        service = _service(source)
        for expression in ("{a} == 'A&&B' && {b} > 0", "!({a} != 'A&&B') || {b} < 0", "{a}=='A&&B'&&!({b}<0)"):
            assert (await service.evaluate(_spec(expression, bindings), t0_ns=0, t1_ns=0)).scalar.value is True

    def test_tokens_inside_a_literal_are_not_extracted(self):
        assert Parser().extract_tokens("{a} == '{b}' and {c} > 0") == ["a", "c"]

    def test_a_literal_that_never_ends_is_a_syntax_error(self):
        with pytest.raises(FormulaParseError, match="Syntax error"):
            Parser().parse("{a} == 'open", {"a": "DEV:A"})


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


class _SlowSource(FakeDataSource):
    """Every read takes ``delay`` seconds; the names in ``stuck`` never answer."""

    delay = 0.0
    stuck: frozenset = frozenset()

    async def read_latest(self, pv: str):
        await asyncio.sleep(3600 if pv in self.stuck else self.delay)
        return self.latest.get(pv)


class TestBoundedReads:
    """One evaluation reads its inputs together and does not wait for a slow one for ever."""

    def _many(self, n: int) -> tuple[_SlowSource, dict[str, str]]:
        bindings = {f"{i:04d}": f"DEV:{i:04d}" for i in range(n)}
        return _SlowSource(latest={pv: _sample(1.0) for pv in bindings.values()}), bindings

    async def test_the_members_are_read_together(self):
        source, bindings = self._many(500)
        source.delay = 0.05  # one after the other this would be 25 s
        start = asyncio.get_running_loop().time()
        result = await _scalar(source, "sum(connected({*}))", bindings)
        assert result.value == 500
        assert asyncio.get_running_loop().time() - start < 2.0

    async def test_a_member_that_is_not_read_in_time_is_without_a_value(self):
        source, bindings = self._many(5)
        source.stuck = frozenset({"DEV:0002"})
        start = asyncio.get_running_loop().time()
        result = await _scalar(source, "sum(connected({*}))", bindings, vector_read_timeout_ms=100)
        assert asyncio.get_running_loop().time() - start < 1.0
        assert result.value == 4
        assert result.severity == AlarmSeverity.INVALID
        assert result.vector_inputs.missing == ["DEV:0002"]

    async def test_a_plain_token_that_is_not_read_in_time_fails_the_formula(self):
        source, bindings = self._many(2)
        source.stuck = frozenset({"DEV:0001"})
        with pytest.raises(FormulaInputMissing, match="DEV:0001"):
            await _scalar(source, "severity({0000}) + {0001}", bindings, vector_read_timeout_ms=100)

    async def test_the_time_limit_is_a_setting(self):
        assert FormulaEngineConfig().vector_read_timeout_ms == 2000


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


def _only(out: "_Collector", value) -> bool:
    """Results have come and every one is ``value``.

    A change that is noticed while an evaluation is under way is evaluated
    again, so the same result may come twice.
    """
    values = out.values()
    return bool(values) and all(v == value for v in values)


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
            await _wait_for(lambda: _only(out, 3))
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
            await _wait_for(lambda: _only(out, 3))
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
            await _wait_for(lambda: _only(first, 3) and _only(second, 3))
        finally:
            await manager.shutdown()

    @pytest.mark.parametrize("expression", ["connected({a})", "0 if severity({a}) >= 2 else 1"])
    async def test_a_status_formula_on_one_channel_waits_too(self, expression):
        """No flash of the worst state when a screen with one such light opens."""
        source, notifier, clock = FakeDataSource(), ChangeNotifier(), _Monotonic()
        manager, _ = self._start(source, notifier, clock)
        out = _Collector()
        try:
            await manager.add_subscription("c", "s", _spec(expression, {"a": "DEV:A"}), 0, 0, out.send)
            await asyncio.sleep(0.1)
            assert out.messages == []
            source.latest["DEV:A"] = _sample(1.0)
            notifier.notify("DEV:A")
            await _wait_for(lambda: _only(out, 1))
        finally:
            await manager.shutdown()

    async def test_a_status_formula_on_one_channel_answers_when_the_window_ends(self):
        source, notifier, clock = FakeDataSource(), ChangeNotifier(), _Monotonic()
        manager, _ = self._start(source, notifier, clock)
        out = _Collector()
        try:
            await manager.add_subscription("c", "s", _spec("connected({a})", {"a": "DEV:A"}), 0, 0, out.send)
            await asyncio.sleep(0.1)
            assert out.messages == []
            clock.now += 1.0
            await _wait_for(lambda: out.values() == [0])
        finally:
            await manager.shutdown()

    async def test_a_plain_formula_is_never_held(self):
        source, notifier, clock = _three(), ChangeNotifier(), _Monotonic()
        manager, service = self._start(source, notifier, clock)
        out = _Collector()
        try:
            await manager.add_subscription("c", "s", _spec("{0} + 1", {"0": "DEV:A"}), 0, 0, out.send)
            await _wait_for(lambda: out.values() == [2.0])
            assert service.evaluations == 1
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

    def test_a_table_keeps_the_order_of_its_bindings_in_the_key(self):
        """Only a scalar formula finds its bindings by key alone."""
        from canopy.formula_engine.services.subscription_manager import _compute_key

        def table(bindings):
            return FormulaSpec(
                kind="table", columns={"a": "{a}", "b": "{b}"}, bindings=bindings, index="grid", grid_dt_ns=10**9
            )

        forward, backward = {"a": "DEV:A", "b": "DEV:B"}, {"b": "DEV:B", "a": "DEV:A"}
        assert _compute_key(table(forward), -1, 0) != _compute_key(table(backward), -1, 0)
        assert _compute_key(table(forward), -1, 0) == _compute_key(table(dict(forward)), -1, 0)
        scalar = "{a} + {b}"
        assert _compute_key(_spec(scalar, forward), 0, 0) == _compute_key(_spec(scalar, backward), 0, 0)

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
        source, notifier = _three(a=(0.0, 0)), ChangeNotifier()
        manager, service = _manager(source, notifier, vector_interval_ms=10)
        out = _Collector()
        try:
            await manager.add_subscription("c", "s", _spec("log(min({*}))", THREE), 0, 0, out.send)
            await _wait_for(lambda: len(out.messages) == 1)
            assert out.messages[0]["code"] == ErrorCode.FORMULA_TYPE_ERROR.value
            assert out.messages[0]["retryable"] is False
            # More samples that fail the same way: evaluated, but the error is not sent again.
            for value in (3.0, 5.0):
                source.latest["DEV:B"] = _sample(value)
                notifier.notify("DEV:B")
                await _wait_for(lambda n=service.evaluations: service.evaluations > n)
            await asyncio.sleep(0.05)
            assert len(out.messages) == 1
            source.latest["DEV:A"] = _sample(1.0)
            notifier.notify("DEV:A")
            await _wait_for(lambda: out.values() == [0.0])
            # The same error again is a new state and is sent again.
            source.latest["DEV:A"] = _sample(0.0)
            notifier.notify("DEV:A")
            await _wait_for(lambda: len(out.messages) == 3)
            assert out.messages[2] == out.messages[0]
        finally:
            await manager.shutdown()

    async def test_a_polled_subscription_sends_a_repeated_error_once(self):
        source, notifier = _three(a=(0.0, 0)), ChangeNotifier()
        manager, service = _manager(source, notifier, vector_interval_ms=10)
        out = _Collector()
        try:
            await manager.add_subscription("c", "s", _spec("log(min({*}))", THREE), 0, 0, out.send, interval_ms=10)
            await _wait_for(lambda: service.evaluations >= 4)
            assert len(out.messages) == 1
            source.latest["DEV:A"] = _sample(1.0)
            await _wait_for(lambda: out.values()[:1] == [0.0])
        finally:
            await manager.shutdown()

    async def test_a_member_whose_value_is_nan_is_reported_at_once_and_followed(self):
        source, notifier = _three(), ChangeNotifier()
        source.latest["DEV:B"] = _sample(math.nan)
        manager, _ = _manager(source, notifier, vector_interval_ms=10)
        out = _Collector()
        try:
            await manager.add_subscription("c", "s", _spec("max({*})", THREE), 0, 0, out.send)
            await _wait_for(lambda: len(out.messages) == 1, timeout=0.5)  # no input-wait silence
            assert out.messages[0]["code"] == ErrorCode.FORMULA_INPUT_MISSING.value
            assert out.messages[0]["retryable"] is True
            assert "DEV:B" in out.messages[0]["message"]
            source.latest["DEV:B"] = _sample(2.0)
            notifier.notify("DEV:B")
            await _wait_for(lambda: out.values() == [4.0])
            assert len(out.messages) == 2
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

    async def test_no_member_is_asked_of_the_archive(self):
        """Not one in outage, and not one the live side has never heard of: both are named instead."""
        from canopy.vole.adapter import VoleDataSource

        fetched: list[str] = []
        subscribed: list[str] = []

        async def fetch_window(pv, t0_ns, t1_ns, **kwargs):
            fetched.append(pv)
            return [_sample(9.0, ts_ns=t1_ns - 1)]

        async def subscribe_live(pvs):
            subscribed.extend(pvs)

        buffer = self._buffer(ChangeNotifier())
        await buffer.append("DEV:A", _sample(1.0, ts_ns=self.NOW))
        buffer.open_outage("DEV:B", self.NOW)  # down before its first sample: no ring
        source = VoleDataSource(buffer, fetch_window=fetch_window, subscribe_live=subscribe_live)
        result = await _scalar(source, "sum(connected({*})) * 10 + severity({0})", THREE)
        assert result.value == 10
        assert result.vector_inputs.missing == ["DEV:B", "DEV:C"]
        assert fetched == [] and subscribed == []

    async def test_a_plain_token_is_still_provisioned_as_in_a_plain_formula(self):
        from canopy.vole.adapter import VoleDataSource

        fetched: list[str] = []

        async def fetch_window(pv, t0_ns, t1_ns, **kwargs):
            fetched.append(pv)
            return [_sample(9.0, ts_ns=t1_ns - 1)]

        async def subscribe_live(pvs):
            return None

        buffer = self._buffer(ChangeNotifier())
        source = VoleDataSource(buffer, fetch_window=fetch_window, subscribe_live=subscribe_live)
        result = await _scalar(source, "severity({a}) + {a}", {"a": "DEV:A"})
        assert result.value == 9.0
        assert fetched == ["DEV:A"]


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
