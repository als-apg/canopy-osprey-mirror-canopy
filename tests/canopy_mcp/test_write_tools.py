"""Draft write tools: the check gate, base_rev refusals and the node edits."""

import copy
import json
from pathlib import Path

import pytest
from fastmcp.exceptions import ToolError

from canopy_mcp.merge import merge_patch
from canopy_mcp.store import summarize_description
from canopy_mcp.tools import drafts, _common
from canopy_mcp.config import Settings
from canopy_mcp.catalog import set_catalog, build_catalog
from canopy_mcp.tools.check import run_check
from canopy_mcp.tools.drafts import (
    add_node,
    get_screen,
    new_screen,
    patch_node,
    list_drafts,
    remove_node,
    delete_draft,
)
from tests.canopy_mcp.synthetic_spec import write_spec

FIXTURES = Path(__file__).parent / "fixtures"
TWO_UNKNOWN = FIXTURES / "two-unknown.screen.json"


def _catalog():
    ir_map = {
        t: {"consumes": [], "verbatim": False}
        for t in ("group", "label", "action-button", "pv-button", "embedded-display", "image-view")
    }
    return build_catalog(ir_map)


def _node(nid, ntype="label", props=None, children=None, **extra):
    node = {"id": nid, "type": ntype, "props": props or {}, "geometry": {"x": 0, "y": 0, "width": 10, "height": 10}}
    if children is not None:
        node["children"] = children
    node.update(extra)
    return node


def _doc(*children, title="t", **top):
    doc = {
        "schemaVersion": "1.0",
        "kind": "screen",
        "id": "t",
        "metadata": {"title": title, "source": {"type": "native"}, "size": {"width": 10, "height": 10}},
        "macros": [],
        "root": _node("root", "group", children=list(children)),
    }
    doc.update(top)
    return doc


@pytest.fixture
def settings(tmp_path, monkeypatch):
    s = Settings(base_url="http://canopy.test", drafts_dir=tmp_path / "drafts", spec_dir=write_spec(tmp_path / "spec"))
    monkeypatch.setattr(drafts, "get_settings", lambda: s)
    _common.reset_stores()
    set_catalog(_catalog())
    yield s
    set_catalog(None)
    _common.reset_stores()


@pytest.fixture
def store(settings):
    return _common.get_store(settings)


def _file(settings, draft_id):
    return settings.drafts_dir / "screens" / f"{draft_id}.screen.json"


async def _checked(draft_id, store):
    return (await run_check(draft_id=draft_id, store=store)).to_dict()


def _all(result):
    return [*result["schema"], *result["semantic"], *result["lint"]]


# --- merge_patch (RFC 7396) ---------------------------------------------------------


@pytest.mark.parametrize(
    ("target", "patch", "expected"),
    [
        ({"a": "b"}, {"a": "c"}, {"a": "c"}),
        ({"a": "b"}, {"b": "c"}, {"a": "b", "b": "c"}),
        ({"a": "b"}, {"a": None}, {}),
        ({"a": "b", "b": "c"}, {"a": None}, {"b": "c"}),
        ({"a": ["b"]}, {"a": "c"}, {"a": "c"}),
        ({"a": "c"}, {"a": ["b"]}, {"a": ["b"]}),
        ({"a": {"b": "c"}}, {"a": {"b": "d", "c": None}}, {"a": {"b": "d"}}),
        ({"a": [{"b": "c"}]}, {"a": [1]}, {"a": [1]}),
        (["a", "b"], ["c", "d"], ["c", "d"]),
        ({"a": "b"}, ["c"], ["c"]),
        ({"a": "foo"}, None, None),
        ({"a": "foo"}, "bar", "bar"),
        ({"e": None}, {"a": 1}, {"e": None, "a": 1}),
        ([1, 2], {"a": "b", "c": None}, {"a": "b"}),
        ({}, {"a": {"bb": {"ccc": None}}}, {"a": {"bb": {}}}),
    ],
)
def test_merge_patch_rfc7396_examples(target, patch, expected):
    before = copy.deepcopy(target)
    assert merge_patch(target, patch) == expected
    assert target == before  # the target is never modified in place


# --- new_screen / get_screen / list_drafts --------------------------------------------


async def test_new_screen_writes_and_returns_draft_rev_url(settings, store):
    out = await new_screen("x/y", _doc(_node("a")))
    assert set(out) >= {"draftId", "rev", "url"}
    assert out["draftId"] == "x/y"
    assert out["rev"] == store.rev("x/y")
    assert out["url"] == "http://127.0.0.1:8765/embed/drafts/x/y"
    assert "refused" not in out

    got = await get_screen("x/y")
    assert got["rev"] == out["rev"]
    assert got["screen"]["root"]["children"][0]["id"] == "a"


async def test_new_screen_url_carries_the_url_prefix_behind_a_proxy(settings):
    settings.url_prefix = "/panel/canopy"
    out = await new_screen("x/y", _doc())
    assert out["url"] == "/panel/canopy/embed/drafts/x/y"


async def test_new_screen_title_is_listed_in_manifest_and_list_drafts(settings, store):
    out = await new_screen("x/y", _doc(title="RF Status"))
    [entry] = store.manifest()["screens"]
    assert (entry["id"], entry["title"]) == ("x/y", "RF Status")
    listed = await list_drafts()
    assert listed["drafts"] == [
        {
            "draftId": "x/y",
            "title": "RF Status",
            "description": "",
            "path": "screens/x/y.screen.json",
            "rev": out["rev"],
            "url": "http://127.0.0.1:8765/embed/drafts/x/y",
            "ref": "drafts/x/y",
        }
    ]


async def test_list_drafts_carries_the_first_sentence_of_the_description(settings, store):
    long = "Shows the RF stations, e.g. SR and XFER. Second sentence is left out."
    await new_screen("rf", _doc(title="RF", metadata={"title": "RF", "description": long}))
    await new_screen("plain", _doc(title="Plain"))
    listed = {d["draftId"]: d["description"] for d in (await list_drafts())["drafts"]}
    assert listed == {"plain": "", "rf": "Shows the RF stations, e.g. SR and XFER."}
    # The viewer's manifest does not carry it.
    assert all("description" not in entry for entry in store.manifest()["screens"])


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (None, ""),
        (42, ""),
        ("", ""),
        ("  One line\n  wrapped.  ", "One line wrapped."),
        ("No full stop", "No full stop"),
        ("x" * 300, "x" * 199 + "\u2026"),
    ],
)
def test_summarize_description(text, expected):
    assert summarize_description(text) == expected


async def test_list_drafts_empty_store(settings):
    assert await list_drafts() == {"drafts": []}


async def test_get_screen_missing_and_invalid_id(settings):
    with pytest.raises(ToolError, match="no draft"):
        await get_screen("nope")
    with pytest.raises(ToolError, match="invalid draft id"):
        await get_screen("../x")


async def test_new_screen_invalid_id_writes_nothing(settings, store):
    with pytest.raises(ToolError, match="invalid draft id"):
        await new_screen("a/../b", _doc())
    assert store.list() == []


# --- the check gate -----------------------------------------------------------------


def _assert_refused(out, code, node_id):
    assert out["refused"] is True
    assert out["reason"] == "check_errors"
    assert "rev" not in out
    hits = [e for e in out["errors"] if e["code"] == code]
    assert hits, out["errors"]
    for e in hits:
        assert e["nodeId"] == node_id
        assert e["path"].startswith("/")
        assert e["severity"] == "error"


async def test_invalid_document_writes_nothing_and_names_nodes(settings, store):
    bad = _doc(_node("ok"), {"id": "broken", "type": "label", "props": {}})  # no geometry
    out = await new_screen("x/bad", bad)
    assert out["refused"] is True and out["reason"] == "check_errors"
    assert any(e["nodeId"] == "broken" and e["path"].startswith("/root/children/1") for e in out["errors"])
    assert store.list() == []
    assert not _file(settings, "x/bad").exists()


async def test_duplicate_node_id_refused(settings, store):
    out = await new_screen("x/dup", _doc(_node("same"), _node("same")))
    _assert_refused(out, "duplicate_node_id", "same")
    assert store.list() == []


async def test_javascript_open_webpage_refused(settings, store):
    btn = _node("btn", "action-button", {"actions": [{"type": "open_webpage", "url": "javascript:alert(1)"}]})
    out = await new_screen("x/js", _doc(btn))
    _assert_refused(out, "open_webpage", "btn")
    assert store.list() == []


async def test_macro_without_default_refused(settings, store):
    out = await new_screen("x/m", _doc(macros=[{"name": "P"}]))
    assert out["refused"] is True
    assert [e["code"] for e in out["errors"]] == ["macro_default"]
    assert store.list() == []


async def test_missing_fox_formula_refused(settings, store):
    rule = {"id": "r1", "name": "colour", "targetProperty": "color", "default": "red", "formula": "fox://missing"}
    out = await new_screen("x/f", _doc(_node("lbl", rules=[rule])))
    assert out["refused"] is True
    assert "unresolved_formula" in {e["code"] for e in out["errors"]}
    assert store.list() == []


async def test_pv_button_writes_with_inert_warning(settings, store):
    out = await new_screen("x/pvb", _doc(_node("b", "pv-button")))
    assert "refused" not in out
    assert out["rev"] == store.rev("x/pvb")
    assert any(w["code"] == "write_not_relayed" and w["nodeId"] == "b" for w in out["warnings"])


async def test_new_screen_with_absent_image_writes_and_check_lists_it(settings, store):
    out = await new_screen("x/img", _doc(_node("pic", "image-view", {"src": "images/x.png"})))
    assert "refused" not in out
    assert out["rev"] == store.rev("x/img")
    checked = await _checked("x/img", store)
    [hit] = [f for f in _all(checked) if f["code"] == "unresolved_ref"]
    assert hit["nodeId"] == "pic"
    assert (hit["kind"], hit["reason"], hit["severity"]) == ("image", "missing", "warning")
    assert "screens/x/images/x.png" in hit["message"]


# --- rev refusals -------------------------------------------------------------------


async def test_new_screen_on_existing_id_without_overwrite_refuses(settings, store):
    first = await new_screen("x/y", _doc(title="one"))
    before = _file(settings, "x/y").read_bytes()
    out = await new_screen("x/y", _doc(title="two"))
    assert out["refused"] is True and out["reason"] == "exists"
    assert out["currentRev"] == first["rev"]
    assert _file(settings, "x/y").read_bytes() == before


async def test_overwrite_needs_the_current_base_rev(settings, store):
    first = await new_screen("x/y", _doc(title="one"))
    before = _file(settings, "x/y").read_bytes()

    missing = await new_screen("x/y", _doc(title="two"), overwrite=True)
    assert missing["refused"] is True and missing["reason"] == "stale_rev"
    stale = await new_screen("x/y", _doc(title="two"), overwrite=True, base_rev="0" * 64)
    assert stale["refused"] is True and stale["reason"] == "stale_rev"
    assert _file(settings, "x/y").read_bytes() == before

    ok = await new_screen("x/y", _doc(title="two"), overwrite=True, base_rev=first["rev"])
    assert ok["rev"] != first["rev"]
    assert (await get_screen("x/y"))["screen"]["metadata"]["title"] == "two"


async def test_patch_node_with_stale_base_rev_refuses(settings, store):
    first = await new_screen("x/y", _doc(_node("a")))
    before = _file(settings, "x/y").read_bytes()
    out = await patch_node("x/y", "a", "f" * 64, {"geometry": {"x": 5}})
    assert out["refused"] is True and out["reason"] == "stale_rev"
    assert out["currentRev"] == first["rev"]
    assert _file(settings, "x/y").read_bytes() == before


async def test_patch_node_with_missing_base_rev_refuses(settings, store):
    await new_screen("x/y", _doc(_node("a")))
    before = _file(settings, "x/y").read_bytes()
    out = await patch_node("x/y", "a", "", {"geometry": {"x": 5}})
    assert out["refused"] is True and out["reason"] == "stale_rev"
    assert _file(settings, "x/y").read_bytes() == before


async def test_store_reverifies_rev_under_its_lock(settings, store, monkeypatch):
    """A draft that changes while the (slow) check runs is not overwritten."""
    first = await new_screen("x/y", _doc(_node("a")))
    racer = _doc(_node("a"), title="racer")
    real_run_check = drafts.run_check
    calls = []

    async def racing_run_check(*args, **kwargs):
        if not calls:
            _file(settings, "x/y").write_text(json.dumps(racer))
        calls.append(1)
        return await real_run_check(*args, **kwargs)

    monkeypatch.setattr(drafts, "run_check", racing_run_check)
    out = await patch_node("x/y", "a", first["rev"], {"geometry": {"x": 5}})
    assert out["refused"] is True and out["reason"] == "stale_rev"
    assert json.loads(_file(settings, "x/y").read_text()) == racer


# --- patch_node ---------------------------------------------------------------------


async def test_patch_node_merges_props_geometry_and_replaces_rules(settings, store):
    rule = {
        "id": "r1",
        "name": "colour",
        "targetProperty": "color",
        "default": "red",
        "pvs": [{"name": "loc://x", "trigger": True}],
        "conditions": [{"expression": "pv0 > 1", "value": "green"}],
    }
    node = _node("a", props={"text": "hi", "font": {"size": 10, "bold": True}}, rules=[rule, rule | {"id": "r2"}])
    first = await new_screen("x/y", _doc(node))
    out = await patch_node(
        "x/y",
        "a",
        first["rev"],
        {"props": {"font": {"bold": None}, "text": "ho"}, "geometry": {"x": 42}, "rules": [rule]},
    )
    assert "refused" not in out
    assert out["rev"] == store.rev("x/y")
    got = (await get_screen("x/y"))["screen"]["root"]["children"][0]
    assert got["props"] == {"text": "ho", "font": {"size": 10}}
    assert got["geometry"] == {"x": 42, "y": 0, "width": 10, "height": 10}
    assert got["rules"] == [rule]


async def test_patch_node_can_patch_the_root(settings, store):
    first = await new_screen("x/y", _doc())
    out = await patch_node("x/y", "root", first["rev"], {"geometry": {"width": 99}})
    assert (await get_screen("x/y"))["screen"]["root"]["geometry"]["width"] == 99
    assert out["rev"] != first["rev"]


@pytest.mark.parametrize("bad_set", [{"type": "group"}, {"id": "z"}, {"children": []}, {}, "props"])
async def test_patch_node_rejects_keys_outside_the_patchable_set(settings, store, bad_set):
    first = await new_screen("x/y", _doc(_node("a")))
    before = _file(settings, "x/y").read_bytes()
    with pytest.raises(ToolError):
        await patch_node("x/y", "a", first["rev"], bad_set)
    assert _file(settings, "x/y").read_bytes() == before


async def test_patch_node_unknown_node_and_missing_draft(settings, store):
    first = await new_screen("x/y", _doc(_node("a")))
    with pytest.raises(ToolError, match="no node"):
        await patch_node("x/y", "nope", first["rev"], {"geometry": {"x": 1}})
    with pytest.raises(ToolError, match="no draft"):
        await patch_node("x/none", "a", first["rev"], {"geometry": {"x": 1}})


async def test_patch_node_refuses_an_introduced_error(settings, store):
    btn = _node("btn", "action-button", {"actions": [{"type": "open_webpage", "url": "https://x"}]})
    first = await new_screen("x/y", _doc(btn))
    before = _file(settings, "x/y").read_bytes()
    out = await patch_node(
        "x/y", "btn", first["rev"], {"props": {"actions": [{"type": "open_webpage", "url": "javascript:x"}]}}
    )
    _assert_refused(out, "open_webpage", "btn")
    assert _file(settings, "x/y").read_bytes() == before


async def test_preexisting_errors_do_not_block_and_are_returned(settings, store):
    doc = _doc(_node("dup"), _node("dup"), _node("free"))
    rev = await store.write("x/y", doc, None)  # stored without the gate, like a converted draft
    out = await patch_node("x/y", "free", rev, {"geometry": {"x": 3}})
    assert "refused" not in out
    assert out["rev"] == store.rev("x/y")
    assert [(e["nodeId"], e["code"]) for e in out["errors"]] == [("dup", "duplicate_node_id")]


async def test_two_unknown_widget_fixture_accepts_a_patch_to_one(settings, store):
    doc = json.loads(TWO_UNKNOWN.read_text())
    rev = await store.write("srrf/two", doc, None)
    before = await _checked("srrf/two", store)
    assert sorted(f["nodeId"] for f in _all(before) if f["code"] == "unknown_widget") == ["tank_1", "tank_2"]

    out = await patch_node("srrf/two", "tank_1", rev, {"geometry": {"x": 20}, "props": {"note": "keep"}})
    assert "refused" not in out, out
    stored = (await get_screen("srrf/two"))["screen"]["root"]["children"]
    assert stored[0]["geometry"]["x"] == 20
    assert stored[0]["props"] == {"originalClass": "tank", "note": "keep"}
    assert stored[1] == doc["root"]["children"][1]


async def test_warnings_empty_list_clears_a_converter_note(settings, store):
    doc = json.loads(TWO_UNKNOWN.read_text())
    rev = await store.write("srrf/two", doc, None)
    notes = [f for f in _all(await _checked("srrf/two", store)) if f["code"] == "warning"]
    assert [(f["nodeId"], f["path"]) for f in notes] == [("Label_3", "/root/children/2/warnings/0")]

    out = await patch_node("srrf/two", "Label_3", rev, {"warnings": []})
    assert "refused" not in out
    after = await _checked("srrf/two", store)
    assert not [f for f in _all(after) if f["code"] == "warning"]
    assert (await get_screen("srrf/two"))["screen"]["root"]["children"][2]["warnings"] == []


# --- add_node / remove_node ---------------------------------------------------------


async def test_add_node_appends_to_the_parent(settings, store):
    first = await new_screen("x/y", _doc(_node("g", "group", children=[])))
    out = await add_node("x/y", "g", _node("child"), first["rev"])
    assert "refused" not in out
    g = (await get_screen("x/y"))["screen"]["root"]["children"][0]
    assert [c["id"] for c in g["children"]] == ["child"]


async def test_add_node_creates_children_list(settings, store):
    first = await new_screen("x/y", _doc(_node("leaf")))
    await add_node("x/y", "leaf", _node("c"), first["rev"])
    leaf = (await get_screen("x/y"))["screen"]["root"]["children"][0]
    assert leaf["children"][0]["id"] == "c"


async def test_add_node_duplicate_id_is_refused(settings, store):
    first = await new_screen("x/y", _doc(_node("a")))
    before = _file(settings, "x/y").read_bytes()
    out = await add_node("x/y", "root", _node("a"), first["rev"])
    _assert_refused(out, "duplicate_node_id", "a")
    assert _file(settings, "x/y").read_bytes() == before


async def test_add_node_stale_rev_and_missing_parent(settings, store):
    first = await new_screen("x/y", _doc())
    out = await add_node("x/y", "root", _node("a"), "stale")
    assert out["refused"] is True and out["reason"] == "stale_rev"
    with pytest.raises(ToolError, match="no node"):
        await add_node("x/y", "nope", _node("a"), first["rev"])
    with pytest.raises(ToolError):
        await add_node("x/y", "root", "not a node", first["rev"])


async def test_remove_then_add_replaces_an_unknown_widget(settings, store):
    doc = json.loads(TWO_UNKNOWN.read_text())
    rev = await store.write("srrf/two", doc, None)
    out = await remove_node("srrf/two", "tank_1", rev)
    assert "refused" not in out
    out = await add_node("srrf/two", "root", _node("tank_1", "label", {"text": "tank"}), out["rev"])
    assert "refused" not in out
    codes = [(f["nodeId"], f["code"]) for f in _all(await _checked("srrf/two", store))]
    assert ("tank_1", "unknown_widget") not in codes
    assert ("tank_2", "unknown_widget") in codes


async def test_remove_node_refusals(settings, store):
    first = await new_screen("x/y", _doc(_node("a")))
    with pytest.raises(ToolError, match="root"):
        await remove_node("x/y", "root", first["rev"])
    with pytest.raises(ToolError, match="no node"):
        await remove_node("x/y", "nope", first["rev"])
    out = await remove_node("x/y", "a", "stale")
    assert out["refused"] is True and out["reason"] == "stale_rev"
    assert (await get_screen("x/y"))["screen"]["root"]["children"][0]["id"] == "a"


async def test_remove_node_drops_it_from_its_parent(settings, store):
    first = await new_screen("x/y", _doc(_node("a"), _node("b")))
    out = await remove_node("x/y", "a", first["rev"])
    assert out["rev"] == store.rev("x/y")
    assert [c["id"] for c in (await get_screen("x/y"))["screen"]["root"]["children"]] == ["b"]


# --- delete_draft -------------------------------------------------------------------


async def test_delete_draft_needs_the_current_rev(settings, store):
    first = await new_screen("x/y", _doc())
    stale = await delete_draft("x/y", "stale")
    assert stale["refused"] is True and stale["reason"] == "stale_rev"
    missing = await delete_draft("x/y", "")
    assert missing["refused"] is True and missing["reason"] == "stale_rev"
    assert _file(settings, "x/y").exists()

    out = await delete_draft("x/y", first["rev"])
    assert out == {"draftId": "x/y", "deleted": True}
    assert store.list() == []
    with pytest.raises(ToolError, match="no draft"):
        await delete_draft("x/y", first["rev"])


# --- colour roles: atomic replace and schemaVersion stamping -------------------------

ROLE = {"$role": "text.primary", "fallback": "#202020", "fallbackDark": "#e0e0e0"}
ROLE2 = {"$role": "alarm.major", "fallback": "#ff0000"}


@pytest.fixture
def stamper(monkeypatch):
    """The stamping tests run without the converter: the rule is canopy_mcp's own."""
    import sys

    monkeypatch.setitem(sys.modules, "pydmconverter.scheme", None)  # import raises ImportError


STAMP_CASES = [
    {"p": ROLE},
    {"p": [ROLE2]},
    {"p": {"$role": "x", "fallback": "red"}},
    {"p": [{"$role": "", "fallback": "#000000"}]},
    {"p": {"$role": "x", "fallback": "#000000", "extra": 1}},
    {"p": [[{"$role": "x" * 64, "fallback": "#00000000"}]]},
    {"p": {"$role": "x" * 65, "fallback": "#000000"}},
    {"p": {"$role": "é" * 64, "fallback": "#000000"}},
    {"p": {"$role": "x", "fallback": "#000000", "fallbackDark": "nope"}},
    {"p": ("not", "json")},
    "not a doc",
    None,
]


@pytest.mark.parametrize("doc", STAMP_CASES)
def test_the_stamp_rule_is_the_converters(doc):
    """The local walk and ``pydmconverter.scheme`` stamp every case alike."""
    scheme = pytest.importorskip("pydmconverter.scheme")
    from canopy_mcp.tools.drafts import _stamp_schema_version

    assert _stamp_schema_version(doc) == scheme.stamp_schema_version(doc)


async def _stored(draft_id="x/y"):
    return (await get_screen(draft_id))["screen"]


async def test_patch_role_over_hex_replaces_the_value_whole(settings, store, stamper):
    first = await new_screen("x/y", _doc(_node("a", props={"text": "hi", "foregroundColor": "#123456"})))
    out = await patch_node("x/y", "a", first["rev"], {"props": {"foregroundColor": ROLE}})
    assert "refused" not in out, out
    doc = await _stored()
    assert doc["root"]["children"][0]["props"] == {"text": "hi", "foregroundColor": ROLE}
    assert doc["schemaVersion"] == "1.1"


async def test_patch_role_over_role_does_not_deep_merge(settings, store, stamper):
    first = await new_screen("x/y", _doc(_node("a", props={"foregroundColor": ROLE})))
    assert (await _stored())["schemaVersion"] == "1.1"
    out = await patch_node("x/y", "a", first["rev"], {"props": {"foregroundColor": ROLE2}})
    assert "refused" not in out, out
    # a deep merge would keep ROLE's stale fallbackDark
    assert (await _stored())["root"]["children"][0]["props"]["foregroundColor"] == ROLE2


async def test_patch_hex_over_role_replaces_and_last_role_returns_to_1_0(settings, store, stamper):
    first = await new_screen("x/y", _doc(_node("a", props={"text": "hi", "foregroundColor": ROLE})))
    out = await patch_node("x/y", "a", first["rev"], {"props": {"foregroundColor": "#abcdef"}})
    assert "refused" not in out, out
    doc = await _stored()
    assert doc["root"]["children"][0]["props"] == {"text": "hi", "foregroundColor": "#abcdef"}
    assert doc["schemaVersion"] == "1.0"


async def test_adding_the_first_role_stamps_1_1(settings, store, stamper):
    first = await new_screen("x/y", _doc(_node("a")))
    assert (await _stored())["schemaVersion"] == "1.0"
    out = await add_node("x/y", "root", _node("b", props={"backgroundColor": ROLE2}), first["rev"])
    assert "refused" not in out, out
    assert (await _stored())["schemaVersion"] == "1.1"


async def test_removing_the_last_role_returns_to_1_0(settings, store, stamper):
    first = await new_screen("x/y", _doc(_node("a"), _node("b", props={"backgroundColor": ROLE2})))
    assert (await _stored())["schemaVersion"] == "1.1"
    out = await remove_node("x/y", "b", first["rev"])
    assert "refused" not in out, out
    assert (await _stored())["schemaVersion"] == "1.0"


async def test_new_screen_stamps_a_role_bearing_1_0_document_as_1_1(settings, store, stamper):
    screen = _doc(_node("a", props={"foregroundColor": ROLE}))
    assert screen["schemaVersion"] == "1.0"
    out = await new_screen("x/y", screen)
    assert "refused" not in out, out
    assert (await _stored())["schemaVersion"] == "1.1"
    assert screen["schemaVersion"] == "1.0"  # the caller's document is not mutated


async def test_malformed_role_does_not_stamp_1_1(settings, store, stamper):
    from canopy_mcp.tools.drafts import _stamp_schema_version

    assert _stamp_schema_version({"p": {"$role": "x", "fallback": "red"}}) == "1.0"
    assert _stamp_schema_version({"p": [{"$role": "", "fallback": "#000000"}]}) == "1.0"
    assert _stamp_schema_version({"p": {"$role": "x", "fallback": "#000000", "extra": 1}}) == "1.0"
    assert _stamp_schema_version({"p": [[{"$role": "x" * 64, "fallback": "#00000000"}]]}) == "1.1"
    assert _stamp_schema_version({"p": {"$role": "x" * 65, "fallback": "#000000"}}) == "1.0"
    assert _stamp_schema_version("not a doc") == "1.0"
