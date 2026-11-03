"""``member_key``, driven from the shared member-key vectors.

``fixtures/collections/member-key-vectors.json`` is authored here. The viewer cannot import
canopy, so it keeps a byte-identical copy and replays the same cases; the file's bytes are
part of the contract: ASCII-only, no trailing whitespace, exactly one final newline.
"""

import json
from pathlib import Path

import pytest

from canopy_mcp.member_key import (
    InvalidMember,
    member_key,
    canonical_macros,
    is_reserved_macro,
)

VECTORS_PATH = Path(__file__).parent / "fixtures" / "collections" / "member-key-vectors.json"
VECTORS_BYTES = VECTORS_PATH.read_bytes()
VECTORS = json.loads(VECTORS_BYTES)
CASES = {case["name"]: case for case in VECTORS["cases"]}


# --- the file itself ---------------------------------------------------------------------


def test_the_vector_file_is_ascii_only() -> None:
    assert VECTORS_BYTES.isascii()


def test_the_vector_file_has_no_trailing_whitespace() -> None:
    assert b"\r" not in VECTORS_BYTES and b"\t" not in VECTORS_BYTES
    assert [n for n, line in enumerate(VECTORS_BYTES.split(b"\n"), 1) if line != line.rstrip()] == []


def test_the_vector_file_ends_in_exactly_one_newline() -> None:
    assert VECTORS_BYTES.endswith(b"\n") and not VECTORS_BYTES.endswith(b"\n\n")


def test_the_vector_file_has_at_least_twenty_cases_with_unique_names() -> None:
    assert len(VECTORS["cases"]) >= 20
    assert len(CASES) == len(VECTORS["cases"])


def test_every_case_has_exactly_one_outcome() -> None:
    for case in VECTORS["cases"]:
        assert ("expect" in case) != ("refused" in case), case["name"]
        assert case.get("refused", True) is True, case["name"]


def test_the_vectors_cover_the_carried_characters() -> None:
    """r3:IA-4: '~', '*', space, '+', an astral name and every reserved name appear in a macro."""
    keyed = [case["member"].get("macros", {}) for case in CASES.values() if "expect" in case]
    names = {name for macros in keyed for name in macros}
    values = "".join(value for macros in keyed for value in macros.values())
    text = "".join(names) + values
    for char in "~* +":
        assert char in text, char
    assert any(ord(char) > 0xFFFF for name in names for char in name)
    assert {"embedded", "theme", "mode"} <= names
    assert any(name.startswith("_canopy") for name in names)


# --- the cases ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", list(CASES))
def test_member_key_vector(name: str) -> None:
    case = CASES[name]
    if case.get("refused"):
        with pytest.raises(InvalidMember):
            member_key(case["member"])
    else:
        assert member_key(case["member"]) == case["expect"]


# --- beyond the vectors (Python-only inputs) ---------------------------------------------


def test_a_non_mapping_member_is_refused() -> None:
    with pytest.raises(InvalidMember):
        member_key(["screen", "library/a"])  # type: ignore[arg-type]


def test_a_lone_surrogate_in_a_macro_is_refused() -> None:
    with pytest.raises(InvalidMember):
        member_key({"screen": "library/a", "macros": {"x": "\ud800"}})


def test_canonical_macros_of_nothing_is_blank() -> None:
    assert canonical_macros({}) == ""
    assert canonical_macros({"theme": "dark"}) == ""


@pytest.mark.parametrize(
    ("name", "reserved"),
    [("embedded", True), ("theme", True), ("mode", True), ("_canopy", True), ("_canopyX", True), ("Mode", False)],
)
def test_is_reserved_macro(name: str, reserved: bool) -> None:
    assert is_reserved_macro(name) is reserved
