"""Sandbox safety tests: every dangerous Python construct must be rejected."""

import pytest

from canopy.formula_engine.errors import FormulaParseError, FormulaUnsafeError
from canopy.formula_engine.services.parser import Parser


@pytest.fixture
def parser():
    return Parser()


DANGEROUS_EXPRESSIONS = [
    ("{a}.__class__", "dunder attribute access"),
    ("{a}.__class__.__bases__", "dunder chain"),
    ("getattr({a}, 'x')", "getattr"),
    ("eval({a})", "eval"),
    ("exec({a})", "exec"),
    ("compile({a}, '', 'exec')", "compile"),
    ("open({a})", "open builtin"),
    ("input({a})", "input builtin"),
    ("breakpoint()", "breakpoint"),
    ("type({a})", "type"),
    ("vars({a})", "vars"),
    ("globals()", "globals"),
    ("locals()", "locals"),
    ("dir({a})", "dir"),
    ("delattr({a}, 'x')", "delattr"),
    ("setattr({a}, 'x', 1)", "setattr"),
]


@pytest.mark.parametrize("expr,desc", DANGEROUS_EXPRESSIONS, ids=[d for _, d in DANGEROUS_EXPRESSIONS])
def test_dangerous_expression_rejected(parser, expr, desc):
    """Every dangerous construct must raise FormulaUnsafeError or FormulaParseError."""
    with pytest.raises((FormulaUnsafeError, FormulaParseError)):
        parser.parse(expr, {"a": "PV:A"})


def test_lambda_rejected(parser):
    with pytest.raises((FormulaUnsafeError, FormulaParseError)):
        parser.parse("(lambda x: x)({a})", {"a": "PV:A"})


def test_list_comprehension_rejected(parser):
    with pytest.raises((FormulaUnsafeError, FormulaParseError)):
        parser.parse("[x for x in {a}]", {"a": "PV:A"})


def test_dict_comprehension_rejected(parser):
    with pytest.raises((FormulaUnsafeError, FormulaParseError)):
        parser.parse("{x: x for x in {a}}", {"a": "PV:A"})


def test_subscript_rejected(parser):
    with pytest.raises((FormulaUnsafeError, FormulaParseError)):
        parser.parse("{a}[0]", {"a": "PV:A"})


def test_import_rejected(parser):
    with pytest.raises((FormulaUnsafeError, FormulaParseError)):
        parser.parse("__import__('os').system('ls')", {"a": "PV:A"})


def test_nested_braces_rejected(parser):
    with pytest.raises(FormulaParseError, match="Nested braces"):
        parser.parse("{a*{b}}", {"a": "PV:A", "b": "PV:B"})


def test_safe_functions_allowed(parser):
    """Verify that known-safe functions pass validation."""
    safe_exprs = [
        ("sin({a})", {"a": "PV:A"}),
        ("cos({a}) + sqrt({b})", {"a": "PV:A", "b": "PV:B"}),
        ("abs({a})", {"a": "PV:A"}),
        ("{a} ** 2", {"a": "PV:A"}),
        ("exp({a})", {"a": "PV:A"}),
        ("log({a})", {"a": "PV:A"}),
    ]
    for expr, bindings in safe_exprs:
        result = parser.parse(expr, bindings)
        assert result.tokens
