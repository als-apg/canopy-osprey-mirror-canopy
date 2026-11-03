"""Every rule condition and formula converted from a Phoebus ``.bob`` must parse in the Formula Engine.

The procedure is fixed: replace every ``${NAME}`` macro reference by ``0``; when no
``{N}`` token remains, check the text with ``ast.parse`` only (the runtime constant-folds
such a ladder, and the Formula Engine parser rejects token-less text); otherwise call
``Parser.parse`` binding exactly the tokens present (the Formula Engine rejects unused bindings).

The corpus test is gated on ``PHOEBUS_CORPUS``; the curated glob and exclusion list
below are kept in step with pydmconverter's ``tests/bob/corpus_manifest.py``. A corpus
copied off its host may hold symlinks that point at the deployed tree; set
``PHOEBUS_CORPUS_DEPLOYED_PREFIX`` to that tree's absolute path (with a trailing
slash) to read such a link from the same relative path under ``PHOEBUS_CORPUS``.
Unset, a dangling link is skipped.
"""

import os
import re
import ast
from fnmatch import fnmatch
from pathlib import Path
from collections import Counter

import pytest
from pydmconverter.react import convert_bytes

from canopy.formula_engine.errors import FormulaParseError, FormulaEngineError
from canopy.formula_engine.services.parser import Parser

CORPUS_ENV = "PHOEBUS_CORPUS"
GLOB = "**/*.bob"
EXCLUDE: tuple[str, ...] = ("examples/*", "autogen/*", "templates/*", "synced_files*/*")
DEPLOYED_PREFIX_ENV = "PHOEBUS_CORPUS_DEPLOYED_PREFIX"

PARSED = "formula-engine-parsed"
AST_ONLY = "ast-only (no tokens)"
FAILED = "failed"

_MACRO_RE = re.compile(r"\$\{[^}]*\}")
_PARSER = Parser()


def check_expression(expression: str) -> tuple[str, str | None]:
    """Classify one condition/formula: ``(outcome, error or None)``."""
    text = _MACRO_RE.sub("0", expression)
    tokens = _PARSER.extract_tokens(text)
    if not tokens:
        try:
            ast.parse(text, mode="eval")
        except SyntaxError as exc:
            return FAILED, f"ast: {exc.msg}"
        return AST_ONLY, None
    try:
        _PARSER.parse(text, {tok: f"PV{tok}" for tok in set(tokens)})
    except FormulaEngineError as exc:
        return FAILED, f"formula engine: {exc}"
    return PARSED, None


# -- unconditional: the three branches ------------------------------------------------


def test_condition_with_tokens_goes_through_formula_engine():
    assert check_expression("{0} == 1 and {1} > 2") == (PARSED, None)


def test_macro_only_condition_is_ast_checked():
    # The Formula Engine itself refuses the token-less text, which is why this branch exists.
    with pytest.raises(FormulaParseError):
        _PARSER.parse("0 == 1", {})
    assert check_expression("${CODE} == 1") == (AST_ONLY, None)


def test_broken_condition_is_a_failure():
    outcome, error = check_expression("{0} == ")
    assert outcome == FAILED
    assert error and error.startswith("formula engine:")
    outcome, error = check_expression("${CODE} ==")
    assert outcome == FAILED
    assert error and error.startswith("ast:")


# -- corpus ---------------------------------------------------------------------------


def _curated(root: Path, deployed_prefix: str = "") -> list[tuple[str, Path]]:
    """``(relative path, file to read)`` for every curated panel, sorted."""
    entries: list[tuple[str, Path]] = []
    for path in sorted(root.glob(GLOB)):
        rel = path.relative_to(root).as_posix()
        if any(fnmatch(rel, pattern) for pattern in EXCLUDE):
            continue
        if path.is_symlink() and not path.exists():
            target = os.readlink(path)
            if not deployed_prefix or not target.startswith(deployed_prefix):
                continue
            candidate = root / target[len(deployed_prefix) :]
            if candidate.is_file():
                entries.append((rel, candidate))
            continue
        if path.is_file():
            entries.append((rel, path))
    return entries


def _nodes(node):
    yield node
    for child in node.children:
        yield from _nodes(child)


def _expressions(screen) -> list[tuple[str, str]]:
    """``(kind, expression)`` for every rule condition and formula in ``screen``."""
    found = [("condition", c.expression) for n in _nodes(screen.root) for r in n.rules for c in r.conditions]
    found += [("formula", f.expression) for f in screen.formulas]
    return found


def test_corpus_rules_and_formulas_parse_in_formula_engine():
    value = os.environ.get(CORPUS_ENV, "").strip()
    if not value:
        pytest.skip(f"{CORPUS_ENV} not set; the Phoebus corpus is not available")
    root = Path(value).expanduser()
    entries = _curated(root, os.environ.get(DEPLOYED_PREFIX_ENV, "").strip())
    assert entries, f"no curated .bob files under {root}"

    outcomes: Counter[tuple[str, str]] = Counter()
    failures: list[str] = []
    for rel, source in entries:
        screen = convert_bytes(source.read_bytes(), kind="bob")
        for kind, expression in _expressions(screen):
            outcome, error = check_expression(expression)
            outcomes[(kind, outcome)] += 1
            if outcome == FAILED:
                failures.append(f"{rel}: {kind} {expression!r} -> {error}")

    summary = "\n".join(
        f"  {kind:9} {outcome:22} {outcomes[(kind, outcome)]}"
        for kind in ("condition", "formula")
        for outcome in (PARSED, AST_ONLY, FAILED)
    )
    assert not failures, (
        f"{len(failures)} expressions fail to parse over {len(entries)} curated panels:\n{summary}\n"
        + "\n".join(failures[:25])
    )
