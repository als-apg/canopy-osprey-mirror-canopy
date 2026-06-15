"""Expression parser: text → validated ParsedFormula.

Steps:
1. Regex-lift ``{...}`` tokens → placeholder identifiers (``__v0``, ``__v1``, ...)
2. Validate all tokens have corresponding bindings; reject nested braces
3. ``ast.parse`` the placeholder-substituted expression
4. Walk AST against ALLOWED_NODES + SAFE_FUNCS
5. Detect numexpr compatibility
6. Return ParsedFormula
"""

import re
import ast
from functools import lru_cache
from dataclasses import dataclass

from canopy.fox.errors import FormulaParseError, FormulaUnsafeError
from canopy.fox.language.grammar import check_ast_safety
from canopy.fox.language.namespace import (
    SAFE_FUNCS,
    NUMEXPR_FUNCS,
    SAFE_CONSTANTS,
    NUMEXPR_NAME_MAP,
)

_TOKEN_RE = re.compile(r"\{([^{}]+)\}")
_NESTED_BRACE_RE = re.compile(r"\{[^}]*\{")


@dataclass(frozen=True)
class ParsedFormula:
    """Result of a successful parse."""

    expression: str
    tokens: list[str]
    placeholder_expr: str
    is_numexpr_compatible: bool


class Parser:
    """Stateless expression parser with LRU caching."""

    def __init__(self, max_expression_len: int = 2048) -> None:
        self._max_expression_len = max_expression_len

    def parse(self, expression: str, bindings: dict[str, str]) -> ParsedFormula:
        """Parse and validate an expression against its bindings."""
        if len(expression) > self._max_expression_len:
            raise FormulaParseError(
                f"Expression exceeds maximum length ({len(expression)} > {self._max_expression_len})"
            )
        return _cached_parse(expression, frozenset(bindings.keys()))

    def extract_tokens(self, expression: str) -> list[str]:
        """Extract ``{name}`` tokens without full validation."""
        return _TOKEN_RE.findall(expression)


@lru_cache(maxsize=256)
def _cached_parse(expression: str, binding_keys: frozenset[str]) -> ParsedFormula:
    if not expression or not expression.strip():
        raise FormulaParseError("Expression is empty")

    if _NESTED_BRACE_RE.search(expression):
        raise FormulaParseError("Nested braces are not allowed in expressions")

    tokens = _TOKEN_RE.findall(expression)
    if not tokens:
        raise FormulaParseError("Expression must contain at least one {variable} token")

    missing = set(tokens) - binding_keys
    if missing:
        raise FormulaParseError(f"Unbound variables: {sorted(missing)}")

    extra = binding_keys - set(tokens)
    if extra:
        raise FormulaParseError(f"Bindings provided but not used in expression: {sorted(extra)}")

    token_to_placeholder: dict[str, str] = {}
    for i, tok in enumerate(dict.fromkeys(tokens)):
        token_to_placeholder[tok] = f"__v{i}"

    placeholder_expr = _TOKEN_RE.sub(lambda m: token_to_placeholder[m.group(1)], expression)

    try:
        tree = ast.parse(placeholder_expr, mode="eval")
    except SyntaxError as exc:
        raise FormulaParseError(f"Syntax error in expression: {exc.msg}") from None

    violations = check_ast_safety(tree)
    if violations:
        raise FormulaUnsafeError(f"Unsafe expression: {'; '.join(violations)}")

    _check_function_calls(tree)

    is_numexpr = _check_numexpr_compatible(tree)

    return ParsedFormula(
        expression=expression,
        tokens=list(dict.fromkeys(tokens)),
        placeholder_expr=placeholder_expr,
        is_numexpr_compatible=is_numexpr,
    )


def _check_function_calls(tree: ast.AST) -> None:
    """Verify every Call references a function in SAFE_FUNCS."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func_name = _resolve_call_name(node)
            if func_name is None:
                raise FormulaUnsafeError("Complex function calls are not allowed")
            if func_name not in SAFE_FUNCS and func_name not in SAFE_CONSTANTS:
                raise FormulaUnsafeError(f"Function '{func_name}' is not allowed")


def _resolve_call_name(node: ast.Call) -> str | None:
    """Extract the function name from a Call node, or None if too complex."""
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        if isinstance(node.func.value, ast.Name) and node.func.value.id in ("np", "math"):
            return node.func.attr
    return None


def _check_numexpr_compatible(tree: ast.AST) -> bool:
    """Return True if the expression uses only numexpr-supported operations."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func_name = _resolve_call_name(node)
            if func_name is None:
                return False
            ne_name = NUMEXPR_NAME_MAP.get(func_name, func_name)
            if ne_name not in NUMEXPR_FUNCS:
                return False
        if isinstance(node, ast.IfExp | ast.BoolOp):
            return False
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
            return False
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.FloorDiv):
            return False
    return True
