"""``validate_collection``: the one collection rule set, and ``configured_tools``.

Every rule of :mod:`canopy_mcp.collections`'s module text has a passing and a refused
case here; the store, ``PUT`` and ``--check`` all call this function. The layout rules
are C-LAYOUT's collection form, and the C-CASES ``validate``/``normalize`` cases copied
into ``fixtures/dock-cases`` replay against :func:`layout_refusal` and
:func:`normalize_layout` (their pins: ``test_dock_cases_pin.py``).
"""

import copy
import json
from pathlib import Path

import pytest

from canopy_mcp.views import MAX_QUERY_BYTES as VIEWS_MAX_QUERY_BYTES
from canopy_mcp.config import Settings
from canopy_mcp.static import TOOL_IDS, viewer_config, configured_tools
from canopy_mcp.listing import Judged, scan, judge
from canopy_mcp.collections import (
    MAX_MEMBERS,
    LAYOUT_REASONS,
    MAX_QUERY_BYTES,
    MAX_LAYOUT_BYTES,
    MAX_COLLECTION_BYTES,
    InvalidLayout,
    OldShapeLayout,
    InvalidCollection,
    layout_refusal,
    library_screens,
    normalize_layout,
    validate_collection,
)

FACILITY_DOC = {
    "id": "storage-ring",
    "name": "  Storage ring  ",
    "description": "Everything for the ring.",
    "members": [
        {"screen": "library/sr/overview"},
        {"screen": "library/sr/rf", "macros": {"SECTOR": "3"}},
        {"tool": "trace", "query": "pv=SR:DCCT", "name": "Beam current"},
        {"tool": "waveform", "name": "Septum pulse"},
    ],
    "layout": {
        "split": "row",
        "items": [
            {"tabs": [0], "size": 2},
            {"split": "column", "items": [{"tabs": [1, 2], "showing": 2}, {"tabs": [3], "size": 0.5}]},
        ],
    },
    "default": True,
    "updated": "2026-10-16T00:00:00.000Z",
}

PERSONAL_DOC = {
    "id": "mine",
    "name": "Mine",
    "members": [{"screen": "drafts/scratch"}, {"tool": "trace"}, {"tool": "trace", "query": ""}],
    "updated": "2026-10-16T00:00:00.000Z",
}


def facility(**changes):
    doc = copy.deepcopy(FACILITY_DOC)
    doc.update(changes)
    return doc


def personal(**changes):
    doc = copy.deepcopy(PERSONAL_DOC)
    doc.update(changes)
    return doc


def refused(doc, match: str, **kwargs) -> None:
    kwargs.setdefault("source", "personal")
    with pytest.raises(InvalidCollection, match=match):
        validate_collection(doc, **kwargs)


def nest(depth: int) -> dict:
    """A layout ``depth`` splits deep, alternating direction, placing members 0..depth."""
    node: dict = {"split": "row" if depth % 2 else "column", "items": [{"tabs": [depth - 1]}, {"tabs": [depth]}]}
    for level in range(depth - 2, -1, -1):
        node = {"split": "row" if level % 2 else "column", "items": [{"tabs": [level]}, node]}
    return node


def screens(count: int) -> list[dict]:
    return [{"screen": f"drafts/p{index}"} for index in range(count)]


# --- valid documents ---------------------------------------------------------------------


def test_a_full_facility_collection_passes_with_its_name_stripped() -> None:
    out = validate_collection(FACILITY_DOC, source="facility")
    assert out["name"] == "Storage ring"
    assert out["members"] == FACILITY_DOC["members"]
    assert out["layout"] == FACILITY_DOC["layout"]
    assert out["default"] is True
    assert list(out) == ["id", "name", "description", "members", "layout", "default", "updated"]


def test_a_personal_collection_may_name_drafts_and_unnamed_tools() -> None:
    out = validate_collection(PERSONAL_DOC, source="personal")
    assert out["members"] == PERSONAL_DOC["members"]


def test_a_facility_file_may_leave_out_id_and_updated() -> None:
    doc = facility()
    del doc["id"], doc["updated"]
    out = validate_collection(doc, source="facility", file_id="storage-ring")
    assert out["id"] == "storage-ring"
    assert "updated" not in out


def test_an_id_field_must_equal_the_file_id() -> None:
    refused(facility(), "file id", source="facility", file_id="other")


def test_an_empty_member_list_is_legal() -> None:
    assert validate_collection(personal(members=[]), source="personal")["members"] == []


def test_a_collection_without_a_layout_is_legal() -> None:
    doc = personal(members=screens(3))
    assert "layout" not in doc
    assert "layout" not in validate_collection(doc, source="personal")


def test_the_input_is_not_mutated() -> None:
    doc = facility()
    validate_collection(doc, source="facility")
    assert doc == FACILITY_DOC


# --- the document ------------------------------------------------------------------------


@pytest.mark.parametrize("doc", [None, [], "x", 3], ids=["null", "array", "string", "number"])
def test_a_non_object_is_refused(doc) -> None:
    refused(doc, "must be an object")


@pytest.mark.parametrize("bad", ["", "a b", "x" * 65, "a/b", 7], ids=["empty", "space", "long", "slash", "int"])
def test_the_id_grammar(bad) -> None:
    refused(personal(id=bad), "id must match")


def test_the_id_accepts_64_characters() -> None:
    assert validate_collection(personal(id="A" * 64), source="personal")["id"] == "A" * 64


@pytest.mark.parametrize(
    "bad", ["", "   ", "x" * 121, "a\nb", 3, None], ids=["empty", "blank", "long", "control", "int", "null"]
)
def test_the_name_rule(bad) -> None:
    refused(personal(name=bad), "name")


def test_name_and_members_are_required() -> None:
    doc = personal()
    del doc["name"]
    refused(doc, "name is required")
    doc = personal()
    del doc["members"]
    refused(doc, "members is required")


def test_unknown_top_level_keys_are_refused() -> None:
    refused(personal(colour="red"), "unknown key")


def test_a_facility_file_carrying_rev_is_refused() -> None:
    # rev is computed on read, never stored: a file that carries one breaks the key rule.
    refused(facility(rev="0123456789abcdef"), "unknown key", source="facility")
    for key in ("rev", "source", "ref"):
        refused(personal(**{key: "x"}), "unknown key")


def test_description_must_be_a_string() -> None:
    refused(personal(description=3), "description must be a string")


def test_updated_must_be_a_string() -> None:
    refused(personal(updated=3), "updated must be a string")


def test_default_is_facility_only() -> None:
    refused(personal(default=False), "default is legal only in a facility", source="personal")


def test_default_must_be_a_boolean() -> None:
    refused(facility(default="yes"), "true or false", source="facility")


def test_source_must_be_known() -> None:
    with pytest.raises(ValueError, match="source"):
        validate_collection(PERSONAL_DOC, source="team")  # type: ignore[arg-type]


# --- members -----------------------------------------------------------------------------


def test_members_must_be_an_array() -> None:
    refused(personal(members={}), "members must be an array")


@pytest.mark.parametrize(
    "member, match",
    [
        ("drafts/x", "must be an object"),
        ({}, "exactly one of screen and tool"),
        ({"screen": "library/a", "tool": "trace"}, "exactly one of screen and tool"),
        ({"screen": "a"}, "<category>/<id>"),
        ({"screen": 3}, "<category>/<id>"),
        ({"screen": "views/a"}, "category"),
        ({"screen": "library/"}, "screen id"),
        ({"screen": "library/a//b"}, "screen id"),
        ({"screen": "library/a.b"}, "screen id"),
        ({"screen": "library/a", "name": "x"}, "unknown key"),
        ({"screen": "library/a", "macros": []}, "macros must be an object"),
        ({"screen": "library/a", "macros": {"A": 1}}, "must be a string"),
        ({"tool": "fft"}, "tool must be one of"),
        ({"tool": 3}, "tool must be one of"),
        ({"tool": "trace", "query": 3}, "query"),
        ({"tool": "trace", "name": ""}, "name"),
        ({"tool": "trace", "macros": {}}, "unknown key"),
    ],
)
def test_a_malformed_member_is_refused(member, match) -> None:
    refused(personal(members=[member]), match)


def test_nested_screen_ids_are_legal() -> None:
    doc = personal(members=[{"screen": "library/sr/rf/cavity-1"}])
    assert validate_collection(doc, source="personal")["members"][0]["screen"] == "library/sr/rf/cavity-1"


def test_a_facility_collection_names_no_drafts() -> None:
    doc = facility(members=[{"screen": "drafts/x"}])
    del doc["layout"]
    refused(doc, "library/\\* screens only", source="facility")


def test_a_facility_tool_member_carries_a_name() -> None:
    doc = facility(members=[{"tool": "trace", "query": "pv=X"}])
    del doc["layout"]
    refused(doc, "carries a name", source="facility")


@pytest.mark.parametrize("tool", TOOL_IDS)
def test_every_static_tool_id_is_legal(tool) -> None:
    assert validate_collection(personal(members=[{"tool": tool}]), source="personal")["members"] == [{"tool": tool}]


def test_tool_ids_do_not_depend_on_what_the_host_serves() -> None:
    # No Settings reach the validator: a host serving no tool page still accepts every tool member.
    doc = facility(members=[{"tool": "correlation", "name": "C"}])
    del doc["layout"]
    assert validate_collection(doc, source="facility")["members"][0]["tool"] == "correlation"


def test_the_query_limit_is_the_views_limit() -> None:
    assert MAX_QUERY_BYTES is VIEWS_MAX_QUERY_BYTES
    at = personal(members=[{"tool": "trace", "query": "x" * MAX_QUERY_BYTES}])
    assert validate_collection(at, source="personal")
    refused(personal(members=[{"tool": "trace", "query": "x" * (MAX_QUERY_BYTES + 1)}]), "4096")


def test_the_query_limit_counts_utf8_bytes() -> None:
    query = "é" * (MAX_QUERY_BYTES // 2 + 1)
    refused(personal(members=[{"tool": "trace", "query": query}]), "UTF-8 bytes")


def test_a_malformed_tool_query_within_the_limit_passes() -> None:
    # Parsing a query is the viewer's concern (resolveMember); Python bounds its size only.
    doc = personal(members=[{"tool": "correlation", "query": "%%%not a query&&&"}])
    assert validate_collection(doc, source="personal")


@pytest.mark.parametrize(
    "members",
    [
        [{"screen": "library/a"}, {"screen": "library/a"}],
        [{"screen": "library/a", "macros": {"X": "1", "theme": "dark"}}, {"screen": "library/a", "macros": {"X": "1"}}],
        [{"screen": "library/a", "macros": {}}, {"screen": "library/a"}],
        [{"tool": "trace", "query": "q", "name": "One"}, {"tool": "trace", "query": "q", "name": "Two"}],
        [{"tool": "trace"}, {"tool": "trace"}],
    ],
    ids=["same-screen", "reserved-macro-dropped", "empty-macros", "name-excluded", "bare-tool"],
)
def test_duplicate_member_keys_are_refused(members) -> None:
    refused(personal(members=members), "duplicates member_key")


@pytest.mark.parametrize(
    "members",
    [
        [{"tool": "trace"}, {"tool": "trace", "query": ""}],
        [{"screen": "library/a"}, {"screen": "drafts/a"}],
        [{"screen": "library/a", "macros": {"X": "1"}}, {"screen": "library/a", "macros": {"X": "2"}}],
    ],
    ids=["absent-vs-blank-query", "category", "macro-value"],
)
def test_distinct_member_keys_pass(members) -> None:
    assert len(validate_collection(personal(members=members), source="personal")["members"]) == 2


def test_at_most_max_members() -> None:
    members = [{"screen": f"drafts/p{index}"} for index in range(MAX_MEMBERS)]
    assert len(validate_collection(personal(members=members), source="personal")["members"]) == MAX_MEMBERS
    refused(personal(members=[*members, {"screen": "drafts/one-more"}]), "MAX_MEMBERS")


# --- the old slot shape ------------------------------------------------------------------


OLD_GRID = {
    "split": "row",
    "items": [{"items": [{"slot": "a"}, {"slot": "c"}], "size": 2}, {"items": [{"slot": "b"}, {"slot": "d"}]}],
    "active": "a",
}


def old_members(count: int = 4) -> list[dict]:
    return [{"screen": f"drafts/p{index}", "slot": "abcd"[index]} for index in range(count)]


def test_the_old_slot_layout_is_its_own_refusal() -> None:
    with pytest.raises(OldShapeLayout) as exc:
        validate_collection(personal(members=old_members(), layout=OLD_GRID), source="personal")
    assert exc.value.reason == "old_slot_shape"
    assert exc.value.where == "layout.items[0].items[0]"
    assert isinstance(exc.value, InvalidLayout) and isinstance(exc.value, InvalidCollection)


def test_the_old_shape_is_named_before_the_member_rules() -> None:
    # A bad member after slotted ones: the old shape is still what is named.
    members = [*old_members(), {"tool": "nope"}]
    with pytest.raises(OldShapeLayout):
        validate_collection(personal(members=members, layout=OLD_GRID), source="personal")


@pytest.mark.parametrize("slot", ["a", "", 3], ids=["text", "empty", "int"])
def test_the_member_slot_is_retired_as_an_unknown_key(slot) -> None:
    doc = personal(members=[{"screen": "drafts/a", "slot": slot}])
    with pytest.raises(OldShapeLayout, match=r"members\[0\]\.slot: old_slot_shape"):
        validate_collection(doc, source="personal")


def test_an_old_key_leaf_is_not_the_old_collection_shape() -> None:
    layout = {"split": "row", "items": [{"key": 0}, {"tabs": [1]}]}
    with pytest.raises(InvalidLayout) as exc:
        validate_collection(personal(members=screens(2), layout=layout), source="personal")
    assert exc.value.reason == "old_key_shape"
    assert not isinstance(exc.value, OldShapeLayout)


# --- layout ------------------------------------------------------------------------------


def layout_doc(layout, count: int = 4):
    return personal(members=screens(count), layout=layout)


def reason_of(layout, count: int = 4) -> str:
    with pytest.raises(InvalidLayout) as exc:
        validate_collection(layout_doc(layout, count), source="personal")
    assert exc.value.reason in LAYOUT_REASONS
    assert str(exc.value).startswith(f"{exc.value.where}: {exc.value.reason}: ")
    return exc.value.reason


def test_a_full_facility_layout_passes_unchanged() -> None:
    out = validate_collection(FACILITY_DOC, source="facility")
    assert out["layout"] == FACILITY_DOC["layout"]


@pytest.mark.parametrize("split", ["row", "column"])
def test_a_split_is_row_or_column(split) -> None:
    assert validate_collection(
        layout_doc({"split": split, "items": [{"tabs": [0, 1]}, {"tabs": [2, 3]}]}), source="personal"
    )


def test_a_bad_split_is_refused() -> None:
    assert reason_of({"split": "grid", "items": [{"tabs": [0, 1]}, {"tabs": [2, 3]}]}) == "bad_split"
    assert reason_of({"items": [{"tabs": [0, 1]}, {"tabs": [2, 3]}]}) == "bad_split"


def test_the_root_may_be_a_tile() -> None:
    assert validate_collection(layout_doc({"tabs": [0, 1, 2, 3]}), source="personal")


def test_a_nested_split_may_run_either_way() -> None:
    same = {"split": "row", "items": [{"tabs": [0]}, {"split": "row", "items": [{"tabs": [1]}, {"tabs": [2, 3]}]}]}
    assert validate_collection(layout_doc(same), source="personal")


def test_showing_defaults_to_the_first_tab_and_names_a_tab() -> None:
    assert validate_collection(layout_doc({"tabs": [2, 0, 1, 3]}), source="personal")
    assert validate_collection(layout_doc({"tabs": [2, 0, 1, 3], "showing": 3}), source="personal")
    assert reason_of({"tabs": [0, 1, 2, 3], "showing": 9}) == "showing_not_in_tabs"
    assert reason_of({"tabs": [0, 1, 2, 3], "showing": "0"}) == "showing_not_in_tabs"
    assert reason_of({"tabs": [0, 1, 2, 3], "showing": True}) == "showing_not_in_tabs"


@pytest.mark.parametrize(
    "layout, reason",
    [
        ({"split": "row", "items": [{"tabs": [0, 1, 2, 3]}]}, "split_too_few_items"),
        ({"split": "row", "items": "x"}, "split_too_few_items"),
        (
            {"split": "row", "items": [{"tabs": [0, 1, 2]}, {"split": "column", "items": [{"tabs": [3]}]}]},
            "split_too_few_items",
        ),
    ],
    ids=["root", "not-a-list", "nested"],
)
def test_every_split_holds_two_items(layout, reason) -> None:
    assert reason_of(layout) == reason


@pytest.mark.parametrize(
    "key",
    [4, -1, 1.5, "0", True, None],
    ids=["out-of-range", "negative", "fraction", "text", "bool", "null"],
)
def test_a_tab_is_a_member_index(key) -> None:
    assert reason_of({"tabs": [0, 1, 2, key]}) == "bad_key"


def test_an_integral_float_index_counts_as_the_index() -> None:
    assert validate_collection(layout_doc({"tabs": [0, 1, 2, 3.0]}), source="personal")


def test_a_tile_lists_at_least_one_tab() -> None:
    assert reason_of({"split": "row", "items": [{"tabs": []}, {"tabs": [0, 1, 2, 3]}]}) == "empty_tabs"
    assert reason_of({"split": "row", "items": [{"tabs": 0}, {"tabs": [1, 2, 3]}]}) == "empty_tabs"


def test_every_member_is_placed_exactly_once() -> None:
    assert reason_of({"split": "row", "items": [{"tabs": [0, 1]}, {"tabs": [1, 2, 3]}]}) == "duplicate_key"
    assert reason_of({"split": "row", "items": [{"tabs": [0, 1]}, {"tabs": [2]}]}) == "member_missing"


def test_a_layout_with_no_members_places_nothing() -> None:
    assert reason_of({"tabs": [0]}, 0) == "bad_key"


@pytest.mark.parametrize("size", [0, -1, "1", True, None, float("inf"), float("nan")])
def test_size_is_a_positive_weight(size) -> None:
    assert reason_of({"split": "row", "items": [{"tabs": [0, 1], "size": size}, {"tabs": [2, 3]}]}) == "bad_size"


def test_weights_need_not_sum_to_one() -> None:
    layout = {"split": "row", "items": [{"tabs": [0, 1], "size": 7}, {"tabs": [2, 3], "size": 3}], "size": 2}
    assert validate_collection(layout_doc(layout), source="personal")


@pytest.mark.parametrize(
    "item",
    [{"tabs": [3], "title": "x"}, {"tabs": [3], "tile": "t1"}, {"tabs": [3], "from": "t1"}],
    ids=["title", "tile-id", "from"],
)
def test_a_tile_carries_only_tabs_showing_and_size(item) -> None:
    assert reason_of({"split": "row", "items": [{"tabs": [0, 1, 2]}, item]}) == "unknown_field"


def test_a_split_carries_only_split_items_and_size() -> None:
    assert reason_of({"split": "row", "items": [{"tabs": [0, 1]}, {"tabs": [2, 3]}], "active": 0}) == "unknown_field"


@pytest.mark.parametrize("layout", [[], "row", 3, None, {"size": 1}], ids=["list", "text", "number", "null", "no-tabs"])
def test_a_layout_node_is_a_tile_or_a_split(layout) -> None:
    assert reason_of(layout) == "not_layout"


def test_the_first_broken_rule_in_tree_order_wins() -> None:
    layout = {"split": "row", "items": [{"tabs": [0], "showing": 1}, {"tabs": [1, 9]}]}
    assert reason_of(layout) == "showing_not_in_tabs"


def test_there_is_no_depth_cap() -> None:
    depth = 30
    assert validate_collection(personal(members=screens(depth + 1), layout=nest(depth)), source="personal")


# --- the C-CASES validate and normalize cases --------------------------------------------

DOCK_CASES = Path(__file__).parent / "fixtures" / "dock-cases"
SIZE_EPSILON = 0.005


def _cases(kind: str) -> list[tuple[str, dict]]:
    return [
        (path.stem, json.loads(path.read_text(encoding="ascii"))) for path in sorted((DOCK_CASES / kind).glob("*.json"))
    ]


def _approx(actual, expected, path: str = "$") -> list[str]:
    """The runtime runner's ``diffApprox``: numbers within ``SIZE_EPSILON``, all else exact."""
    number = (int, float)
    if isinstance(expected, number) and not isinstance(expected, bool):
        ok = isinstance(actual, number) and not isinstance(actual, bool) and abs(actual - expected) <= SIZE_EPSILON
        return [] if ok else [f"{path}: {actual!r} != {expected!r}"]
    if isinstance(expected, list):
        if not isinstance(actual, list) or len(actual) != len(expected):
            return [f"{path}: {actual!r} != {expected!r}"]
        return [line for i, item in enumerate(expected) for line in _approx(actual[i], item, f"{path}[{i}]")]
    if isinstance(expected, dict):
        if not isinstance(actual, dict) or set(actual) != set(expected):
            return [f"{path}: keys {sorted(actual) if isinstance(actual, dict) else actual!r} != {sorted(expected)}"]
        return [line for key in sorted(expected) for line in _approx(actual[key], expected[key], f"{path}.{key}")]
    return [] if type(actual) is type(expected) and actual == expected else [f"{path}: {actual!r} != {expected!r}"]


def test_the_cases_cover_both_forms_and_every_reason_the_collection_form_can_give() -> None:
    validate = [case for _, case in _cases("validate")]
    forms = {case.get("options", {}).get("form", "dock") for case in validate}
    assert forms == {"dock", "collection"}
    collection = {
        case["reason"] for case in validate if case.get("options", {}).get("form") == "collection" and "reason" in case
    }
    assert {"old_slot_shape", "member_missing", "duplicate_key", "bad_key", "unknown_field", "bad_size"} <= collection
    assert {case.get("form", "dock") for _, case in _cases("normalize")} == {"dock", "collection"}


@pytest.mark.parametrize("name, case", _cases("validate"), ids=lambda value: value if isinstance(value, str) else "")
def test_each_validate_case_replays(name, case) -> None:
    options = case.get("options", {})
    refusal = layout_refusal(case["input"], form=options.get("form", "dock"), members=options.get("members"))
    got = "valid" if refusal is None else refusal[0]
    assert got == ("valid" if "valid" in case else case["reason"]), name


@pytest.mark.parametrize("name, case", _cases("normalize"), ids=lambda value: value if isinstance(value, str) else "")
def test_each_normalize_case_replays(name, case) -> None:
    before = copy.deepcopy(case["input"])
    assert _approx(normalize_layout(case["input"], form=case.get("form", "dock")), case["output"]) == [], name
    assert case["input"] == before


def test_the_collection_validator_agrees_with_every_collection_case_that_has_members() -> None:
    for name, case in _cases("validate"):
        options = case.get("options", {})
        if options.get("form") != "collection" or "members" not in options:
            continue
        doc = personal(members=screens(options["members"]), layout=case["input"])
        if "valid" in case:
            assert validate_collection(doc, source="personal")["layout"] == case["input"], name
        else:
            with pytest.raises(InvalidLayout) as exc:
                validate_collection(doc, source="personal")
            assert exc.value.reason == case["reason"], name


# --- aggregate limits --------------------------------------------------------------------


def test_the_layout_is_at_most_32_kib() -> None:
    assert MAX_LAYOUT_BYTES == 32 * 1024
    members = screens(2)
    # A 4000-digit weight is a legal positive number; nine of them pass 32 KiB of JSON.
    heavy = int("9" * 4000)
    near = {"split": "row", "items": [{"tabs": [0], "size": heavy}] * 7 + [{"tabs": [1]}]}
    over = {"split": "row", "items": [{"tabs": [0], "size": heavy}] * 9 + [{"tabs": [1]}]}
    # The near layout fails only on the repeated tab, i.e. after the size bound passed.
    refused(personal(members=members, layout=near), "duplicate_key")
    refused(personal(members=members, layout=over), "MAX_LAYOUT_BYTES")


def test_a_heavy_weight_is_legal() -> None:
    layout = {"split": "row", "items": [{"tabs": [0], "size": int("9" * 400)}, {"tabs": [1]}]}
    assert validate_collection(personal(members=screens(2), layout=layout), source="personal")


def test_the_whole_collection_is_at_most_256_kib() -> None:
    members = [{"tool": "trace", "query": f"{index:03d}" + "x" * (MAX_QUERY_BYTES - 3)} for index in range(MAX_MEMBERS)]
    assert validate_collection(personal(members=members), source="personal")
    refused(personal(members=members, description="d" * 70_000), "MAX_COLLECTION_BYTES")


def test_the_size_names_the_total() -> None:
    with pytest.raises(InvalidCollection, match=r"is \d+ bytes, at most 262144"):
        validate_collection(personal(description="d" * MAX_COLLECTION_BYTES), source="personal")


# --- library membership ------------------------------------------------------------------


def make_library(root: Path) -> Path:
    for rel in ("screens/sr/overview.screen.json", "screens/sr/rf.screen.json", "screens/logo.png"):
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}")
    return root


def test_library_screens_lists_screens_only(tmp_path: Path) -> None:
    root = make_library(tmp_path / "lib")
    assert library_screens(root) == {"sr/overview", "sr/rf"}
    assert library_screens(judge(scan(root))) == {"sr/overview", "sr/rf"}
    assert library_screens(str(root)) == {"sr/overview", "sr/rf"}


def test_with_a_library_screen_members_must_be_listed(tmp_path: Path) -> None:
    root = make_library(tmp_path / "lib")
    doc = facility()
    out = validate_collection(doc, source="facility", library=root)
    assert out["members"][0]["screen"] == "library/sr/overview"
    doc["members"][0]["screen"] = "library/sr/missing"
    refused(doc, "is not a screen the library lists", source="facility", library=root)


def test_an_image_never_matches_a_screen_member(tmp_path: Path) -> None:
    root = make_library(tmp_path / "lib")
    refused(personal(members=[{"screen": "library/logo"}]), "library lists", library=root)


def test_a_refused_library_entry_is_not_listed() -> None:
    # A case collision is refused while its files stay listed (and cannot be made on a
    # case-insensitive disk), so the judgement is built by hand; a refused directory
    # above a screen hides it the same way.
    judged = Judged(
        files=(("screens/dup.screen.json", ""), ("screens/sr/rf.screen.json", ""), ("screens/ok.screen.json", "")),
        refusals=(
            {"path": "screens/dup.screen.json", "code": "case_collision"},
            {"path": "screens/sr", "code": "case_collision"},
        ),
    )
    assert library_screens(judged) == {"ok"}
    refused(personal(members=[{"screen": "library/dup"}]), "library lists", library=judged)
    refused(personal(members=[{"screen": "library/sr/rf"}]), "library lists", library=judged)


def test_drafts_members_are_not_checked_against_the_library(tmp_path: Path) -> None:
    root = make_library(tmp_path / "lib")
    assert validate_collection(personal(), source="personal", library=root)


def test_without_a_library_any_library_id_passes() -> None:
    assert validate_collection(personal(members=[{"screen": "library/anything"}]), source="personal")


# --- configured_tools --------------------------------------------------------------------


def tool_settings(tmp_path: Path, pages: tuple[str, ...] | None) -> Settings:
    drafts = tmp_path / "drafts"
    drafts.mkdir(exist_ok=True)
    dist = None
    if pages is not None:
        dist = tmp_path / "dist"
        dist.mkdir(exist_ok=True)
        for page in pages:
            (dist / page).write_text("<html></html>")
    return Settings(drafts_dir=drafts, trace_dist=dist, pv_mode="hare", api_key="k")


def test_the_static_tool_tuple() -> None:
    assert TOOL_IDS == ("trace", "correlation", "waveform")


@pytest.mark.parametrize(
    "pages, expected",
    [
        (None, ()),
        ((), ()),
        (("index.html",), ("trace",)),
        (("waveform.html", "index.html"), ("trace", "waveform")),
        (("correlation.html",), ("correlation",)),
        (("index.html", "correlation.html", "waveform.html"), TOOL_IDS),
    ],
    ids=["no-dist", "empty-dist", "trace", "trace-waveform", "correlation-only", "all"],
)
def test_configured_tools_follow_the_served_pages(tmp_path: Path, pages, expected) -> None:
    settings = tool_settings(tmp_path, pages)
    assert configured_tools(settings) == expected
    config = viewer_config(settings)
    assert tuple(tool for tool in TOOL_IDS if tool in config) == expected
