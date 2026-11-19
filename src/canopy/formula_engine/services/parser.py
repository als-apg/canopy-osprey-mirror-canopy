"""Expression parser: text → validated ParsedFormula.

Steps:
1. Regex-lift ``{...}`` tokens → placeholder identifiers (``__v0``, ``__v1``, ...)
2. Validate all tokens have corresponding bindings; reject nested braces
3. ``ast.parse`` the placeholder-substituted expression
4. Walk AST against ALLOWED_NODES + SAFE_FUNCS
5. Detect numexpr compatibility
6. Return ParsedFormula

Two additions serve formulas over many channels:

* A *vector token* names several bindings at once: ``{*}`` every binding,
  ``{g.*}`` the bindings whose key starts with ``g.``. Its placeholder holds an
  array at evaluation time. A token that is itself a binding key is never a
  vector token.
* ``severity({x})`` and ``connected({x})`` read a token's alarm severity and
  connection state instead of its value. Each call is lifted to its own
  placeholder (``__s0``, ``__c0``) before the AST is built, so the argument can
  only ever be one token.

Every rewrite of the text (tokens, status calls, ``&&`` / ``||`` / ``!``) is
made on the code between string literals only (`_split_strings`): the text of
a literal reaches the AST exactly as written.
"""

import re
import ast
from functools import lru_cache
from dataclasses import field, dataclass
from collections.abc import Callable

from canopy.formula_engine.errors import FormulaParseError, FormulaUnsafeError
from canopy.formula_engine.language.grammar import check_ast_safety
from canopy.formula_engine.language.namespace import (
    SAFE_FUNCS,
    NUMEXPR_FUNCS,
    SAFE_CONSTANTS,
    NUMEXPR_NAME_MAP,
)

_TOKEN_RE = re.compile(r"\{([^{}]+)\}")
# ``severity({x})`` / ``connected({x})``: a status function applied to one token.
# ``label({x})``: the enum string of one channel's value. It reads the value,
# so its token is held to the rule of a plain value token.
STATUS_FUNCS: dict[str, str] = {"severity": "__s", "connected": "__c", "label": "__l"}
LABEL_FUNC = "label"
_STATUS_CALL_RE = re.compile(r"(?<![\w.])(severity|connected|label)\s*\(\s*\{([^{}]+)\}\s*\)")
_VECTOR_ALL = "*"
_VECTOR_SUFFIX = ".*"
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
    # Vector token -> the binding keys it names, sorted. Empty for a plain formula.
    vector_tokens: dict[str, tuple[str, ...]] = field(default_factory=dict)
    # (placeholder, function, token) for every distinct ``severity({x})`` /
    # ``connected({x})`` / ``label({x})``.
    status_symbols: tuple[tuple[str, str, str], ...] = ()
    # Tokens whose value the expression reads (they appear outside a status function).
    value_tokens: frozenset[str] = frozenset()
    # Tokens read through ``label()``: no placeholder of their own, but their value is read.
    label_tokens: frozenset[str] = frozenset()
    # Vector tokens whose values the expression reads somewhere outside a
    # ``nan*`` reduction (`_unguarded_vectors`): a member without a number
    # would be taken for a number there.
    unguarded_vectors: frozenset[str] = frozenset()

    @property
    def uses_status(self) -> bool:
        """True when the formula has a vector token or a status function.

        Such a formula is evaluated on the scalar path that tolerates inputs
        without a value; every other formula takes the paths it always took.
        """
        return bool(self.vector_tokens or self.status_symbols)

    @property
    def status_only(self) -> frozenset[str]:
        """Scalar tokens read only through ``severity()`` / ``connected()``."""
        return frozenset(
            t
            for t in self.tokens
            if t not in self.value_tokens and t not in self.vector_tokens and t not in self.label_tokens
        )

    def member_keys(self) -> list[str]:
        """Every binding key a vector token names, each once."""
        return list(dict.fromkeys(key for members in self.vector_tokens.values() for key in members))


def _split_strings(expression: str, *, strict: bool = False) -> list[tuple[bool, str]]:
    """The expression as ``(is_string_literal, text)`` pieces, in order.

    A literal starts at a quote (or three of the same) and ends at the next
    one that no backslash escapes. A ``#`` in code starts a comment, which runs
    to the end of the line and stays code: a quote inside it opens nothing, and
    it is rewritten as it always was (Python then ignores it). Joining the
    pieces gives the expression back.

    A literal that never ends takes the rest; with ``strict`` it is the syntax
    error it is, reported before anything is concluded from the tokens found.
    """
    pieces: list[tuple[bool, str]] = []
    start = i = 0
    n = len(expression)
    while i < n:
        char = expression[i]
        if char == "#":
            newline = expression.find("\n", i)
            i = n if newline < 0 else newline
            continue
        if char not in "'\"":
            i += 1
            continue
        quote = char * 3 if expression.startswith(char * 3, i) else char
        end = i + len(quote)
        while end < n and not expression.startswith(quote, end):
            end += 2 if expression[end] == "\\" else 1
        if end >= n and strict:
            raise FormulaParseError("Syntax error in expression: unterminated string literal")
        end = min(end + len(quote), n)
        if i > start:
            pieces.append((False, expression[start:i]))
        pieces.append((True, expression[i:end]))
        start = i = end
    if start < n:
        pieces.append((False, expression[start:]))
    return pieces


def _in_code(pieces: list[tuple[bool, str]], rewrite: Callable[[str], str]) -> list[tuple[bool, str]]:
    """Apply ``rewrite`` to every piece that is not a string literal."""
    return [(is_string, text if is_string else rewrite(text)) for is_string, text in pieces]


def _code_tokens(pieces: list[tuple[bool, str]]) -> list[str]:
    return [token for is_string, text in pieces if not is_string for token in _TOKEN_RE.findall(text)]


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
        return _code_tokens(_split_strings(expression))


@lru_cache(maxsize=256)
def _cached_parse(expression: str, binding_keys: frozenset[str]) -> ParsedFormula:
    if not expression or not expression.strip():
        raise FormulaParseError("Expression is empty")

    pieces = _split_strings(expression, strict=True)
    if any(_NESTED_BRACE_RE.search(text) for is_string, text in pieces if not is_string):
        raise FormulaParseError("Nested braces are not allowed in expressions")

    tokens = _code_tokens(pieces)
    if not tokens:
        raise FormulaParseError("Expression must contain at least one {variable} token")

    vector_tokens = _vector_tokens(tokens, binding_keys)

    missing = set(tokens) - binding_keys - set(vector_tokens)
    if missing:
        raise FormulaParseError(f"Unbound variables: {sorted(missing)}")

    used = set(tokens).union(*vector_tokens.values())
    extra = binding_keys - used
    if extra:
        raise FormulaParseError(f"Bindings provided but not used in expression: {sorted(extra)}")

    token_index = {tok: i for i, tok in enumerate(dict.fromkeys(tokens))}

    # Lift ``severity({x})`` / ``connected({x})`` to placeholders of their own. A
    # formula without them is substituted exactly as before.
    status_symbols: dict[str, tuple[str, str, str]] = {}

    def _lift_status(match: re.Match[str]) -> str:
        func, token = match.group(1), match.group(2)
        if func == LABEL_FUNC and token in vector_tokens:
            raise FormulaParseError(f"label() takes one channel, not the vector token {{{token}}}")
        name = f"{STATUS_FUNCS[func]}{token_index[token]}"
        status_symbols[name] = (name, func, token)
        return name

    pieces = _in_code(pieces, lambda code: _STATUS_CALL_RE.sub(_lift_status, code))
    value_tokens = frozenset(_code_tokens(pieces))

    pieces = _in_code(pieces, lambda code: _TOKEN_RE.sub(lambda m: f"__v{token_index[m.group(1)]}", code))
    for pattern, replacement in _BOOL_REPLACEMENTS:
        pieces = _in_code(pieces, lambda code, p=pattern, r=replacement: p.sub(r, code))
    placeholder_expr = "".join(text for _, text in pieces)

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

        _check_status_calls(tree)
        _check_function_calls(tree)
        _check_exponents(tree)

        is_numexpr = _check_numexpr_compatible(tree)
        unguarded = _unguarded_vectors(tree, {f"__v{token_index[t]}": t for t in vector_tokens if t in value_tokens})
    except RecursionError:
        raise FormulaParseError("Expression is too complex to parse") from None

    return ParsedFormula(
        expression=expression,
        tokens=list(dict.fromkeys(tokens)),
        placeholder_expr=placeholder_expr,
        is_numexpr_compatible=is_numexpr,
        vector_tokens=vector_tokens,
        status_symbols=tuple(status_symbols.values()),
        value_tokens=value_tokens,
        label_tokens=frozenset(token for _, func, token in status_symbols.values() if func == LABEL_FUNC),
        unguarded_vectors=unguarded,
    )


def _vector_tokens(tokens: list[str], binding_keys: frozenset[str]) -> dict[str, tuple[str, ...]]:
    """The vector tokens among ``tokens`` and the binding keys each one names.

    Members are sorted by key, so the result does not depend on the order the
    bindings were written in and two groups with the same key suffixes pair up
    element by element. A token that is itself a binding key stays a plain token.
    """
    found: dict[str, tuple[str, ...]] = {}
    for token in dict.fromkeys(tokens):
        if token in binding_keys:
            continue
        if token == _VECTOR_ALL:
            members = sorted(binding_keys)
        elif token.endswith(_VECTOR_SUFFIX) and len(token) > len(_VECTOR_SUFFIX):
            prefix = token[: -len(_VECTOR_ALL)]
            members = sorted(key for key in binding_keys if key.startswith(prefix))
        else:
            continue
        if not members:
            raise FormulaParseError(f"Vector token {{{token}}} matches no binding")
        found[token] = tuple(members)
    return found


# What may stand between a vector token and the reduction that guards it: only
# what hands a NaN on. (``**`` does not: ``nan ** 0`` is 1.)
_NAN_GUARDS = frozenset({"nanmax", "nanmin", "nansum", "nanmean", "count"})
_NAN_KEEPING_OPS = (ast.Add, ast.Sub, ast.Mult, ast.Div, ast.UAdd, ast.USub)
_NAN_KEEPING_FUNCS = frozenset(
    {"abs", "sqrt", "exp", "log", "log10", "ln", "floor", "ceil", "radians", "degrees"}
    | {"sin", "cos", "tan", "asin", "acos", "atan", "sinh", "cosh", "tanh"}
)


def _unguarded_vectors(tree: ast.AST, placeholders: dict[str, str]) -> frozenset[str]:
    """The vector tokens with a value use that no ``nan*`` reduction guards.

    A member without a value reads as NaN. A ``nan*`` reduction leaves it out
    and ``count`` does not look at it. Everywhere else it would pass for a
    number: a comparison with NaN is false, ``count_nonzero`` counts NaN as
    nonzero, ``where`` picks a branch. A use is guarded only when nothing but
    arithmetic and element-wise functions that keep a NaN stands between the
    token and such a reduction. Whatever this cannot show to be guarded is
    unguarded.
    """
    if not placeholders:
        return frozenset()
    parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
    unguarded: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id in placeholders and not _is_guarded(node, parents):
            unguarded.add(placeholders[node.id])
    return frozenset(unguarded)


def _is_guarded(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> bool:
    while True:
        parent = parents.get(node)
        if isinstance(parent, ast.Call) and parent.args == [node] and not parent.keywords:
            name = _resolve_call_name(parent)
            if name in _NAN_GUARDS:
                return True
            if name not in _NAN_KEEPING_FUNCS:
                return False
        elif isinstance(parent, ast.BinOp | ast.UnaryOp) and isinstance(parent.op, _NAN_KEEPING_OPS):
            pass
        else:
            return False
        node = parent


def _check_status_calls(tree: ast.AST) -> None:
    """Refuse a ``severity`` / ``connected`` / ``label`` call that was not lifted: its argument is not one token."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and _resolve_call_name(node) in STATUS_FUNCS:
            raise FormulaParseError(
                f"{_resolve_call_name(node)}() takes exactly one token, e.g. {_resolve_call_name(node)}({{x}})"
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
