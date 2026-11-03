"""Parser unit tests: golden table of expressions."""

import pytest

from canopy.formula_engine.errors import FormulaParseError, FormulaUnsafeError
from canopy.formula_engine.services.parser import Parser


@pytest.fixture
def parser():
    return Parser()


class TestValidExpressions:
    def test_simple_addition(self, parser):
        result = parser.parse("{a} + {b}", {"a": "PV:A", "b": "PV:B"})
        assert result.tokens == ["a", "b"]
        assert "__v0" in result.placeholder_expr
        assert "__v1" in result.placeholder_expr

    def test_sqrt_expression(self, parser):
        result = parser.parse("sqrt({x}**2 + {y}**2)", {"x": "PV:X", "y": "PV:Y"})
        assert set(result.tokens) == {"x", "y"}
        assert result.is_numexpr_compatible is True

    def test_single_variable(self, parser):
        result = parser.parse("{a}", {"a": "PV:A"})
        assert result.tokens == ["a"]

    def test_constant_multiplication(self, parser):
        result = parser.parse("{a} * 2.5 + 1", {"a": "PV:A"})
        assert result.tokens == ["a"]

    def test_trig_functions(self, parser):
        result = parser.parse("sin({a}) + cos({b})", {"a": "PV:A", "b": "PV:B"})
        assert result.is_numexpr_compatible is True

    def test_mean_not_numexpr_compatible(self, parser):
        result = parser.parse("mean({a})", {"a": "PV:A"})
        assert result.is_numexpr_compatible is False

    def test_bool_op_not_numexpr_compatible(self, parser):
        result = parser.parse("{a} and {b}", {"a": "PV:A", "b": "PV:B"})
        assert result.is_numexpr_compatible is False

    def test_not_op_not_numexpr_compatible(self, parser):
        result = parser.parse("not {a}", {"a": "PV:A"})
        assert result.is_numexpr_compatible is False

    def test_floordiv_not_numexpr_compatible(self, parser):
        result = parser.parse("{a} // {b}", {"a": "PV:A", "b": "PV:B"})
        assert result.is_numexpr_compatible is False

    def test_duplicate_token(self, parser):
        result = parser.parse("{a} + {a}", {"a": "PV:A"})
        assert result.tokens == ["a"]


class TestParseErrors:
    def test_expression_exceeds_max_length(self):
        short_parser = Parser(max_expression_len=20)
        with pytest.raises(FormulaParseError, match="exceeds maximum length"):
            short_parser.parse("{a} + " * 10, {"a": "PV:A"})

    def test_empty_expression(self, parser):
        with pytest.raises(FormulaParseError, match="empty"):
            parser.parse("", {"a": "PV:A"})

    def test_no_tokens(self, parser):
        with pytest.raises(FormulaParseError, match="at least one"):
            parser.parse("1 + 2", {})

    def test_unbound_variable(self, parser):
        with pytest.raises(FormulaParseError, match="Unbound"):
            parser.parse("{a} + {b}", {"a": "PV:A"})

    def test_extra_bindings(self, parser):
        with pytest.raises(FormulaParseError, match="not used"):
            parser.parse("{a}", {"a": "PV:A", "b": "PV:B"})

    def test_nested_braces(self, parser):
        with pytest.raises(FormulaParseError, match="Nested braces"):
            parser.parse("{a*{b}}", {"a": "PV:A", "b": "PV:B"})

    def test_syntax_error(self, parser):
        with pytest.raises(FormulaParseError, match="Syntax error"):
            parser.parse("{a} +", {"a": "PV:A"})


class TestUnsafeExpressions:
    def test_import(self, parser):
        with pytest.raises(FormulaUnsafeError):
            parser.parse("{a} + __import__('os')", {"a": "PV:A"})

    def test_unknown_function(self, parser):
        with pytest.raises(FormulaUnsafeError, match="not allowed"):
            parser.parse("open({a})", {"a": "PV:A"})

    def test_lambda(self, parser):
        with pytest.raises((FormulaUnsafeError, FormulaParseError)):
            parser.parse("(lambda x: x)({a})", {"a": "PV:A"})

    def test_list_comprehension(self, parser):
        with pytest.raises((FormulaUnsafeError, FormulaParseError)):
            parser.parse("[x for x in {a}]", {"a": "PV:A"})

    def test_dunder_attribute(self, parser):
        with pytest.raises(FormulaUnsafeError):
            parser.parse("{a}.__class__", {"a": "PV:A"})

    def test_dunder_on_allowed_prefix(self, parser):
        with pytest.raises(FormulaUnsafeError, match="Dunder"):
            parser.parse("np.__class__({a})", {"a": "PV:A"})


class TestExponentGuard:
    @pytest.mark.parametrize("expr", ["{a}*0+10**10**10", "{a}*0+2**2**30", "{a}**2000", "{a}*0+2**(5*1000)"])
    def test_large_constant_exponent_rejected(self, parser, expr):
        # A big constant power makes numexpr fold a Python big-int and hang.
        with pytest.raises(FormulaUnsafeError, match="exponent"):
            parser.parse(expr, {"a": "PV:A"})

    @pytest.mark.parametrize("expr", ["{a}**2", "{a}**{b}", "{a}*0+2**1000", "sqrt({a})**3"])
    def test_normal_powers_allowed(self, parser, expr):
        binds = {"a": "PV:A"}
        if "{b}" in expr:
            binds["b"] = "PV:B"
        parser.parse(expr, binds)  # must not raise

    def test_deeply_nested_exponent_maps_to_parse_error(self, parser):
        # A left-recursive BinOp chain in the exponent parses within the length
        # limit but recurses deeply in _const_number. That RecursionError must
        # map to a clean FormulaParseError, not escape as a 500.
        expr = "{a}**(" + "+".join(["1"] * 1000) + ")"
        assert len(expr) <= 2048
        with pytest.raises(FormulaParseError, match="too complex"):
            parser.parse(expr, {"a": "PV:A"})


class TestTernaryExpressions:
    def test_simple_ternary(self, parser):
        result = parser.parse("{a} if {b} > 0 else 0", {"a": "PV:A", "b": "PV:B"})
        assert result.tokens == ["a", "b"]
        assert result.is_numexpr_compatible is False

    def test_nested_ternary(self, parser):
        result = parser.parse(
            "'red' if {sev} > 1 else ('orange' if {x} > 10 else 'green')",
            {"sev": "PV:SEV", "x": "PV:X"},
        )
        assert set(result.tokens) == {"sev", "x"}
        assert result.is_numexpr_compatible is False

    def test_ternary_with_string_output(self, parser):
        result = parser.parse("'high' if {a} > 100 else 'low'", {"a": "PV:A"})
        assert result.tokens == ["a"]


class TestBooleanOperators:
    def test_c_style_and(self, parser):
        result = parser.parse("{a} > 0 && {b} > 0", {"a": "PV:A", "b": "PV:B"})
        assert " and " in result.placeholder_expr

    def test_c_style_or(self, parser):
        result = parser.parse("{a} > 0 || {b} > 0", {"a": "PV:A", "b": "PV:B"})
        assert " or " in result.placeholder_expr

    def test_c_style_not(self, parser):
        result = parser.parse("!{a}", {"a": "PV:A"})
        assert "not " in result.placeholder_expr

    def test_not_equals_preserved(self, parser):
        result = parser.parse("{a} != {b}", {"a": "PV:A", "b": "PV:B"})
        assert "!=" in result.placeholder_expr
