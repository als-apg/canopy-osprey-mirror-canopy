"""The push grammar and the forbidden-character predicate, driven from the shared vectors.

``fixtures/grammar-vectors.json`` is authored here. A second implementation that cannot
import canopy keeps a byte-identical copy and runs the same cases, so the file's bytes
are part of the contract: ASCII-only, no trailing whitespace, exactly one final newline.
"""

import re
import json
from pathlib import Path

import pytest

from canopy_mcp.store import (
    ID_PATTERN,
    ASSET_SEGMENT,
    IMAGE_SUFFIXES,
    RESERVED_ENDINGS,
    RESERVED_PREFIXES,
    RESERVED_SEGMENTS,
    PUSH_IMAGE_SUFFIXES,
    is_forbidden_char,
    has_forbidden_char,
    casefold_collisions,
    is_reserved_segment,
    first_forbidden_pointer,
)

VECTORS_PATH = Path(__file__).parent / "fixtures" / "grammar-vectors.json"
VECTORS_BYTES = VECTORS_PATH.read_bytes()
VECTORS = json.loads(VECTORS_BYTES)
RESERVED = VECTORS["reserved"]
FORBIDDEN = VECTORS["characters"]["forbidden"]
FORBIDDEN_CHARS = [(name, char) for name, chars in FORBIDDEN.items() for char in chars]
POINTER_CLASSES = {case["class"] for case in VECTORS["pointers"]}


def _code(char: str) -> str:
    return f"U+{ord(char):04X}"


# --- the file itself ---------------------------------------------------------------------


def test_the_vector_file_is_ascii_only() -> None:
    assert VECTORS_BYTES.isascii()


def test_the_vector_file_has_no_trailing_whitespace() -> None:
    assert b"\r" not in VECTORS_BYTES and b"\t" not in VECTORS_BYTES
    assert [n for n, line in enumerate(VECTORS_BYTES.split(b"\n"), 1) if line != line.rstrip()] == []


def test_the_vector_file_ends_in_exactly_one_newline() -> None:
    assert VECTORS_BYTES.endswith(b"\n") and not VECTORS_BYTES.endswith(b"\n\n")


# --- patterns and suffix sets ------------------------------------------------------------


def test_the_patterns_are_the_ones_the_vectors_name() -> None:
    assert ID_PATTERN.pattern == "{0}(/{0})*".format(VECTORS["patterns"]["idSegment"])
    assert ASSET_SEGMENT.pattern == VECTORS["patterns"]["assetSegment"]


def test_the_suffix_sets() -> None:
    assert sorted(IMAGE_SUFFIXES) == VECTORS["suffixes"]["image"]
    assert sorted(PUSH_IMAGE_SUFFIXES) == VECTORS["suffixes"]["pushImage"]
    assert PUSH_IMAGE_SUFFIXES == IMAGE_SUFFIXES - {".svg"}


# --- reserved segments -------------------------------------------------------------------


def test_the_reserved_tables() -> None:
    assert sorted(RESERVED_SEGMENTS) == RESERVED["names"]
    assert sorted(RESERVED_PREFIXES) == sorted(RESERVED["prefixes"])
    assert sorted(RESERVED_ENDINGS) == RESERVED["endings"]
    assert all(name == name.casefold() for name in (*RESERVED_SEGMENTS, *RESERVED_PREFIXES, *RESERVED_ENDINGS))


def test_every_reserved_rule_has_a_case() -> None:
    cases = RESERVED["cases"]
    assert {c["segment"].casefold() for c in cases if c["rule"] == "name"} == set(RESERVED["names"])
    for prefix in RESERVED["prefixes"]:
        assert any(c["rule"] == "prefix" and c["segment"].startswith(prefix) for c in cases)
    for ending in RESERVED["endings"]:
        assert any(c["rule"] == "ending" and c["segment"].endswith(ending) for c in cases)
    assert {"__PYCACHE__", "x.SWP"} <= {c["segment"] for c in cases}


@pytest.mark.parametrize("case", RESERVED["cases"], ids=lambda c: f"{c['rule']}:{c['segment']}")
def test_a_reserved_segment_is_reserved_by_its_rule(case: dict) -> None:
    segment = case["segment"]
    folded = segment.casefold()
    assert is_reserved_segment(segment)
    if case["rule"] == "name":
        assert folded in RESERVED_SEGMENTS
    elif case["rule"] == "prefix":
        assert folded.startswith(RESERVED_PREFIXES) and folded not in RESERVED_SEGMENTS
    else:
        assert case["rule"] == "ending"
        assert folded.endswith(RESERVED_ENDINGS) and not folded.startswith(RESERVED_PREFIXES)


@pytest.mark.parametrize("segment", RESERVED["free"])
def test_a_near_miss_is_not_reserved(segment: str) -> None:
    assert not is_reserved_segment(segment)


# --- id and asset segments ---------------------------------------------------------------


def _id_segment(segment: str) -> bool:
    return "/" not in segment and ID_PATTERN.fullmatch(segment) is not None


def _asset_segment(segment: str) -> bool:
    return ASSET_SEGMENT.fullmatch(segment) is not None


SEGMENT_KINDS = {"idSegments": _id_segment, "assetSegments": _asset_segment}


def _accepted() -> list:
    return [pytest.param(kind, s, id=f"{kind}:{s}") for kind in SEGMENT_KINDS for s in VECTORS[kind]["accepted"]]


def _refused() -> list:
    return [
        pytest.param(kind, r["segment"], r["why"], id=f"{kind}:{r['why']}:{r['segment']!r}")
        for kind in SEGMENT_KINDS
        for r in VECTORS[kind]["refused"]
    ]


@pytest.mark.parametrize(("kind", "segment"), _accepted())
def test_a_segment_is_accepted(kind: str, segment: str) -> None:
    assert SEGMENT_KINDS[kind](segment)
    assert not is_reserved_segment(segment)


@pytest.mark.parametrize(("kind", "segment", "why"), _refused())
def test_a_segment_is_refused(kind: str, segment: str, why: str) -> None:
    assert why in ("pattern", "reserved")
    assert SEGMENT_KINDS[kind](segment) is (why == "reserved")
    if why == "reserved":
        assert is_reserved_segment(segment)


@pytest.mark.parametrize("kind", SEGMENT_KINDS)
def test_the_segment_patterns_are_the_stored_ones(kind: str) -> None:
    pattern = re.compile(VECTORS["patterns"][kind.removesuffix("s")])
    for segment in [*VECTORS[kind]["accepted"], *(r["segment"] for r in VECTORS[kind]["refused"])]:
        assert (pattern.fullmatch(segment) is not None) is SEGMENT_KINDS[kind](segment)


# --- the forbidden-character predicate ---------------------------------------------------


def test_every_character_class_is_covered() -> None:
    assert set(FORBIDDEN) == {"c0", "del", "c1", "bidi", "format", "surrogate"}
    assert all(FORBIDDEN.values())
    assert set(FORBIDDEN) <= POINTER_CLASSES


def test_the_character_vectors_are_single_characters() -> None:
    assert all(len(char) == 1 for _, char in FORBIDDEN_CHARS)
    assert all(len(char) == 1 for char in VECTORS["characters"]["allowed"])


@pytest.mark.parametrize(("name", "char"), FORBIDDEN_CHARS, ids=lambda v: v if len(v) > 1 else _code(v))
def test_a_forbidden_character(name: str, char: str) -> None:
    assert is_forbidden_char(char)
    assert has_forbidden_char(f"ab{char}cd")


@pytest.mark.parametrize("char", VECTORS["characters"]["allowed"], ids=_code)
def test_an_allowed_character(char: str) -> None:
    assert not is_forbidden_char(char)
    assert not has_forbidden_char(f"ab{char}cd")


def test_the_c0_and_c1_blocks_in_full() -> None:
    allowed = {"\t", "\n", "\r"}
    for code in [*range(0x00, 0x20), 0x7F, *range(0x80, 0xA0)]:
        assert is_forbidden_char(chr(code)) is (chr(code) not in allowed), _code(chr(code))
    assert not any(is_forbidden_char(chr(code)) for code in range(0x20, 0x7F))


def test_every_surrogate_is_forbidden() -> None:
    assert all(is_forbidden_char(chr(code)) for code in range(0xD800, 0xE000))


def test_an_empty_string_holds_no_forbidden_character() -> None:
    assert not has_forbidden_char("")


# --- the document walk -------------------------------------------------------------------


@pytest.mark.parametrize("case", VECTORS["pointers"], ids=lambda c: c["name"])
def test_the_first_offending_pointer(case: dict) -> None:
    assert first_forbidden_pointer(case["doc"]) == case["pointer"]


def test_the_walk_does_not_change_the_document() -> None:
    for case in VECTORS["pointers"]:
        before = json.dumps(case["doc"])
        first_forbidden_pointer(case["doc"])
        assert json.dumps(case["doc"]) == before


# --- casefold uniqueness -----------------------------------------------------------------


@pytest.mark.parametrize("case", VECTORS["casefold"], ids=lambda c: c["name"])
def test_casefold_collisions(case: dict) -> None:
    assert casefold_collisions(case["paths"]) == case["collisions"]
    assert casefold_collisions(reversed(case["paths"])) == case["collisions"]


def test_the_casefold_vectors_hold_a_file_pair_and_a_directory_pair() -> None:
    names = {case["name"] for case in VECTORS["casefold"]}
    assert {"a file pair", "a directory pair"} <= names
