"""``validate_collection``: the one collection rule set, and ``configured_tools``.

Every rule of :mod:`canopy_mcp.collections`'s module text has a passing and a refused
case here; the store, ``PUT`` and ``--check`` all call this function.
"""

import copy
from pathlib import Path

import pytest

from canopy_mcp.views import MAX_QUERY_BYTES as VIEWS_MAX_QUERY_BYTES
from canopy_mcp.config import Settings
from canopy_mcp.static import TOOL_IDS, viewer_config, configured_tools
from canopy_mcp.listing import Judged, scan, judge
from canopy_mcp.collections import (
    MAX_MEMBERS,
    MAX_QUERY_BYTES,
    MAX_LAYOUT_BYTES,
    MAX_LAYOUT_DEPTH,
    MAX_COLLECTION_BYTES,
    InvalidCollection,
    library_screens,
    validate_collection,
)

FACILITY_DOC = {
    "id": "storage-ring",
    "name": "  Storage ring  ",
    "description": "Everything for the ring.",
    "members": [
        {"screen": "library/sr/overview", "slot": "a"},
        {"screen": "library/sr/rf", "macros": {"SECTOR": "3"}, "slot": "b"},
        {"tool": "trace", "query": "pv=SR:DCCT", "name": "Beam current", "slot": "c"},
        {"tool": "waveform", "name": "Septum pulse"},
    ],
    "layout": {
        "split": "row",
        "items": [{"slot": "a", "size": 2}, {"items": [{"slot": "b"}, {"slot": "c", "size": 0.5}]}],
        "active": "b",
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
    """A layout ``depth`` splits deep, with slots s0..s<depth>."""
    node: dict = {"items": [{"slot": f"s{depth - 1}"}, {"slot": f"s{depth}"}]}
    for level in range(depth - 2, -1, -1):
        node = {"items": [{"slot": f"s{level}"}, node]}
    return {"split": "column", **node}


def slotted(count: int) -> list[dict]:
    return [{"screen": f"drafts/p{index}", "slot": f"s{index}"} for index in range(count)]


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


def test_members_without_a_leaf_are_legal() -> None:
    doc = personal(members=slotted(3), layout={"split": "row", "items": [{"slot": "s0"}, {"slot": "s1"}]})
    assert validate_collection(doc, source="personal")["members"][2]["slot"] == "s2"


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


# --- slots -------------------------------------------------------------------------------


@pytest.mark.parametrize("bad", ["", "a b", "x" * 65, 3], ids=["empty", "space", "long", "int"])
def test_the_slot_grammar(bad) -> None:
    refused(personal(members=[{"screen": "drafts/a", "slot": bad}]), "slot must match")


def test_slots_are_unique() -> None:
    refused(personal(members=[{"screen": "drafts/a", "slot": "x"}, {"tool": "trace", "slot": "x"}]), "not unique")


# --- layout ------------------------------------------------------------------------------


def layout_doc(layout, count: int = 4):
    return personal(members=slotted(count), layout=layout)


def test_the_root_split_is_row_or_column() -> None:
    for split in ("row", "column"):
        assert validate_collection(
            layout_doc({"split": split, "items": [{"slot": "s0"}, {"slot": "s1"}]}), source="personal"
        )
    refused(layout_doc({"split": "grid", "items": [{"slot": "s0"}, {"slot": "s1"}]}), "row' or 'column")
    refused(layout_doc({"items": [{"slot": "s0"}, {"slot": "s1"}]}), "row' or 'column")


def test_the_layout_must_be_an_object() -> None:
    refused(layout_doc([]), "layout must be an object")


def test_only_the_root_names_split() -> None:
    layout = {"split": "row", "items": [{"slot": "s0"}, {"split": "column", "items": [{"slot": "s1"}, {"slot": "s2"}]}]}
    refused(layout_doc(layout), "only the layout root")


def test_only_the_root_names_active() -> None:
    layout = {"split": "row", "items": [{"slot": "s0"}, {"active": "s1", "items": [{"slot": "s1"}, {"slot": "s2"}]}]}
    refused(layout_doc(layout), "only the layout root")


@pytest.mark.parametrize(
    "layout",
    [
        {"split": "row", "items": [{"slot": "s0"}]},
        {"split": "row", "items": []},
        {"split": "row", "items": "s0"},
        {"split": "row", "items": [{"slot": "s0"}, {"items": [{"slot": "s1"}]}]},
    ],
    ids=["root-one", "root-empty", "root-not-array", "nested-one"],
)
def test_every_split_holds_two_items(layout) -> None:
    refused(layout_doc(layout), "at least two items")


@pytest.mark.parametrize(
    "size",
    [0, -1, True, "1", float("inf"), float("nan"), None],
    ids=["zero", "negative", "bool", "string", "inf", "nan", "null"],
)
def test_size_is_a_positive_number(size) -> None:
    refused(layout_doc({"split": "row", "items": [{"slot": "s0", "size": size}, {"slot": "s1"}]}), "positive number")


def test_a_nested_split_may_carry_a_size() -> None:
    layout = {"split": "row", "items": [{"slot": "s0"}, {"size": 3, "items": [{"slot": "s1"}, {"slot": "s2"}]}]}
    assert validate_collection(layout_doc(layout), source="personal")
    layout["items"][1]["size"] = 0
    refused(layout_doc(layout), "positive number")


def test_depth_is_at_most_six() -> None:
    assert validate_collection(layout_doc(nest(MAX_LAYOUT_DEPTH), MAX_LAYOUT_DEPTH + 1), source="personal")
    refused(layout_doc(nest(MAX_LAYOUT_DEPTH + 1), MAX_LAYOUT_DEPTH + 2), "deeper than 6")


@pytest.mark.parametrize(
    "item, match",
    [
        ({"slot": "nope"}, "name the slot of a member"),
        ({"slot": 3}, "name the slot of a member"),
        ({"slot": "s1", "items": [{"slot": "s2"}, {"slot": "s3"}]}, "not both"),
        ({"size": 1}, "slot or items"),
        ("s1", "must be an object"),
        ({"slot": "s1", "weight": 1}, "unknown key"),
        ({"items": [{"slot": "s1"}, {"slot": "s2"}], "slot2": 1}, "unknown key"),
    ],
)
def test_a_malformed_layout_item_is_refused(item, match) -> None:
    refused(layout_doc({"split": "row", "items": [{"slot": "s0"}, item]}), match)


def test_a_leaf_cannot_name_an_unslotted_member() -> None:
    doc = personal(
        members=[{"screen": "drafts/a", "slot": "a"}, {"screen": "drafts/b"}],
        layout={"split": "row", "items": [{"slot": "a"}, {"slot": "b"}]},
    )
    refused(doc, "name the slot of a member")


def test_a_slot_is_placed_once() -> None:
    refused(layout_doc({"split": "row", "items": [{"slot": "s0"}, {"slot": "s0"}]}), "placed twice")


def test_unknown_root_keys_are_refused() -> None:
    refused(layout_doc({"split": "row", "items": [{"slot": "s0"}, {"slot": "s1"}], "size": 1}), "unknown key")


def test_active_names_a_member_slot() -> None:
    base = {"split": "row", "items": [{"slot": "s0"}, {"slot": "s1"}]}
    # A slotted member without a leaf (s3, undocked) may be active.
    assert validate_collection(layout_doc({**base, "active": "s3"}), source="personal")
    refused(layout_doc({**base, "active": "zz"}), "layout.active")
    refused(layout_doc({**base, "active": 1}), "layout.active")


# --- aggregate limits --------------------------------------------------------------------


def test_the_layout_is_at_most_32_kib() -> None:
    assert MAX_LAYOUT_BYTES == 32 * 1024
    members = slotted(2)
    # A 4000-digit weight is a legal positive number; nine of them pass 32 KiB of JSON.
    heavy = int("9" * 4000)
    near = {"split": "row", "items": [{"slot": "s0", "size": heavy}] * 7 + [{"slot": "s1"}]}
    over = {"split": "row", "items": [{"slot": "s0", "size": heavy}] * 9 + [{"slot": "s1"}]}
    # The near layout fails only on the repeated leaf, i.e. after the size bound passed.
    refused(personal(members=members, layout=near), "placed twice")
    refused(personal(members=members, layout=over), "MAX_LAYOUT_BYTES")


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
