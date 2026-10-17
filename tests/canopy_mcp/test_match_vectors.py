"""The shared screen-matcher vectors (match-vectors.json) are well formed.

canopy authors the vectors but implements no matcher; the runtime and OSPREY run them.
These tests pin the file's shape so every holder can rely on it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

VECTORS_PATH = Path(__file__).parent / "fixtures" / "collections" / "match-vectors.json"
VECTORS_BYTES = VECTORS_PATH.read_bytes()
VECTORS = json.loads(VECTORS_BYTES)
CASES = {case["name"]: case for case in VECTORS["cases"]}

KIND_FIELDS = {
    "rank": {"name", "kind", "query", "candidates", "expect"},
    "include": {"name", "kind", "manifest", "showSubScreens", "expect"},
}


# --- the file itself ---------------------------------------------------------------------


def test_the_vector_file_is_ascii_only() -> None:
    assert VECTORS_BYTES.isascii()


def test_the_vector_file_has_no_trailing_whitespace() -> None:
    assert b"\r" not in VECTORS_BYTES and b"\t" not in VECTORS_BYTES
    assert [n for n, line in enumerate(VECTORS_BYTES.split(b"\n"), 1) if line != line.rstrip()] == []


def test_the_vector_file_ends_in_exactly_one_newline() -> None:
    assert VECTORS_BYTES.endswith(b"\n") and not VECTORS_BYTES.endswith(b"\n\n")


def test_the_vector_file_has_about_and_cases_only() -> None:
    assert set(VECTORS) == {"about", "cases"}
    assert VECTORS["about"] and all(isinstance(line, str) for line in VECTORS["about"])


def test_case_names_are_unique() -> None:
    assert len(CASES) == len(VECTORS["cases"])


def test_both_kinds_are_present() -> None:
    assert {case["kind"] for case in VECTORS["cases"]} == set(KIND_FIELDS)


def test_the_vectors_cover_the_spec() -> None:
    rank = [case for case in VECTORS["cases"] if case["kind"] == "rank"]
    include = [case for case in VECTORS["cases"] if case["kind"] == "include"]
    # empty query -> nothing; the camelCase forms all find GTLView; a multi-token query
    assert any(case["query"].strip() == "" and case["expect"] == [] for case in rank)
    for query in ("view", "gtl view", "gtlv"):
        assert any(case["query"] == query and case["expect"] == ["GTLView"] for case in rank), query
    assert any(case["query"] == "sr overview" for case in rank)
    # a keyword-only match listed after a label match
    keyword_only = [
        case for case in rank if any(c["label"] in case["expect"][1:] and c["keywords"] for c in case["candidates"])
    ]
    assert keyword_only
    # sub-screens hidden then shown; '@' categories and non-ir kinds excluded
    assert {False, True} <= {case["showSubScreens"] for case in include}
    assert any(m["category"].startswith("@") for case in include for m in case["manifest"])
    assert any(m["kind"] != "ir" for case in include for m in case["manifest"])


# --- the cases ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", list(CASES))
def test_case_shape(name: str) -> None:
    case = CASES[name]
    assert case["kind"] in KIND_FIELDS, f"unknown kind {case['kind']!r}"
    assert set(case) == KIND_FIELDS[case["kind"]]
    assert isinstance(case["expect"], list)
    assert all(isinstance(item, str) for item in case["expect"])
    assert len(set(case["expect"])) == len(case["expect"])
    if case["kind"] == "rank":
        assert isinstance(case["query"], str)
        labels = [c["label"] for c in case["candidates"]]
        for candidate in case["candidates"]:
            assert set(candidate) == {"label", "keywords"}
            assert all(isinstance(k, str) for k in candidate["keywords"])
        assert set(case["expect"]) <= set(labels)
    else:
        assert isinstance(case["showSubScreens"], bool)
        ids = [m["id"] for m in case["manifest"]]
        for entry in case["manifest"]:
            assert set(entry) == {"id", "category", "kind"}
        # include keeps manifest order
        assert case["expect"] == [i for i in ids if i in case["expect"]]
