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

from canopy.formula_engine.errors import FormulaParseError, FormulaUnsafeError
from canopy.formula_engine.language.grammar import check_ast_safety
from canopy.formula_engine.language.namespace import (
    SAFE_FUNCS,
    NUMEXPR_FUNCS,
    SAFE_CONSTANTS,
    NUMEXPR_NAME_MAP,
)

_TOKEN_RE = re.compile(r"\{([^{}]+)\}")
_NESTED_BRACE_RE = re.compile(r"\{[^}]*\{")

# Reject constant exponents above this. numexpr folds a constant power like
# ``10**10**10`` into a Python big-int before evaluating, which pins a CPU for
# many seconds (and to_thread workers can't be cancelled). asteval guards its
# own scalar path; this guards the numexpr array path. No real PV formula needs
# a literal exponent this large.
_MAX_CONST_EXPONENT = 1024

_BOOL_REPLACEMENTS = [
    (re.compile(r"&&"), " and "),
    (re.compile(r"\|\|"), " or "),
    (re.compile(r"!(?!=)"), "not "),
]


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

    for pattern, replacement in _BOOL_REPLACEMENTS:
        placeholder_expr = pattern.sub(replacement, placeholder_expr)

    try:
        tree = ast.parse(placeholder_expr, mode="eval")
    except SyntaxError as exc:
        raise FormulaParseError(f"Syntax error in expression: {exc.msg}") from None
    except (MemoryError, RecursionError):
        # CPython's parser raises these (not SyntaxError) on pathologically deep
        # nesting. Reachable only if max_expression_len is raised, but map it to
        # a clean 4xx rather than a 500 regardless.
        raise FormulaParseError("Expression is too complex to parse") from None

    # The post-parse analysis recurses over the AST (notably _const_number,
    # which walks left-recursive BinOp chains). An expression that parses within
    # the length limit can still nest deeply enough to blow the Python stack here
    # (e.g. ``{x}**(1+1+...+1)``), so the RecursionError guard has to cover this
    # block too, not just ast.parse - otherwise it escapes as a 500.
    try:
        violations = check_ast_safety(tree)
        if violations:
            raise FormulaUnsafeError(f"Unsafe expression: {'; '.join(violations)}")

        _check_function_calls(tree)
        _check_exponents(tree)

        is_numexpr = _check_numexpr_compatible(tree)
    except RecursionError:
        raise FormulaParseError("Expression is too complex to parse") from None

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


def _const_number(node: ast.AST) -> float | int | None:
    """Best-effort static value of a small numeric constant expression, else None.

    Deliberately bounded: it refuses to compute values from large operands so it
    can't itself be turned into the big-int blowup it exists to detect. Anything
    involving a variable (e.g. ``x``) or an operator it doesn't fold returns None.
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, int | float) and not isinstance(node.value, bool):
        return node.value
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        inner = _const_number(node.operand)
        return None if inner is None else -inner
    if isinstance(node, ast.BinOp):
        left = _const_number(node.left)
        right = _const_number(node.right)
        if left is None or right is None:
            return None
        if isinstance(node.op, ast.Add):
            return left + right
        if isinstance(node.op, ast.Sub):
            return left - right
        if isinstance(node.op, ast.Mult):
            if abs(left) > 1e9 or abs(right) > 1e9:
                return None
            return left * right
        if isinstance(node.op, ast.Pow):
            # Only fold small powers; a large one is exactly what we reject, so
            # don't compute it here.
            if abs(right) > 64 or abs(left) > 1e9:
                return None
            return left**right
    return None


def _check_exponents(tree: ast.AST) -> None:
    """Reject a power whose exponent is a constant above the safe bound."""
    for node in ast.walk(tree):
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Pow):
            exponent = _const_number(node.right)
            if exponent is not None and abs(exponent) > _MAX_CONST_EXPONENT:
                raise FormulaUnsafeError(f"Constant exponent {exponent:g} exceeds the maximum of {_MAX_CONST_EXPONENT}")


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
