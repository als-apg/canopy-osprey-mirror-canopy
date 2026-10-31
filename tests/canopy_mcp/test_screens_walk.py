"""The screen walk: ``canopy_mcp.screens`` against the shared expand() vectors.

``fixtures/expand-vectors.json`` is a byte copy of the sdk's
``packages/screen-ir/test-vectors/expand.json``, which the runtime generates from
its ``expandScreen``. Every case is run here with the same harness rules: each
document is written to ``screens/<key>.screen.json`` of a real store, ``docsRaw``
texts are written verbatim, and ``screenPath`` a key means ``top_on_path=True``.
The rest pins what canopy adds to the walk: ``include_hidden``, trees and
listings. The write-binding rows built on it are ``test_write_bindings``'.
"""

import json
from pathlib import Path

import pytest

from canopy_mcp import screens
from canopy_mcp.store import DraftStore
from canopy_mcp.channels import MAPPED_TYPES
from canopy_mcp.check.refs import StoreTree, BufferTree

VECTORS_PATH = Path(__file__).parent / "fixtures" / "expand-vectors.json"
VECTORS = json.loads(VECTORS_PATH.read_bytes())
CASES = VECTORS["cases"]
VECTOR_NODE_KEYS = ("screen", "instance", "nodeId", "type", "props", "ruleChannels", "machineBound")


def _write_case(root: Path, case: dict) -> DraftStore:
    for key, doc in case["docs"].items():
        path = root / "screens" / f"{key}.screen.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(doc), encoding="utf-8")
    for key, text in case["docsRaw"].items():
        path = root / "screens" / f"{key}.screen.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return DraftStore(root)


def _run(case: dict, root: Path):
    store = _write_case(root, case)
    if case["screenPath"] is not None:
        assert case["screenPath"] == case["top"], "the harness only puts the top itself on the path"
    kwargs = {"macros": case["macros"], "top_on_path": case["screenPath"] is not None}
    top = case["docs"][case["top"]]
    return (
        screens.expand(top, store, case["top"], **kwargs),
        screens.expanded_pvs(top, store, case["top"], **kwargs),
    )


# --- the vectors -------------------------------------------------------------


def test_mapped_types_equal_the_vectors():
    assert sorted(MAPPED_TYPES) == VECTORS["mappedTypes"]
    assert "embedded-display" not in MAPPED_TYPES
    assert "absolute-canvas" not in MAPPED_TYPES


def test_every_case_has_a_unique_name():
    names = [case["name"] for case in CASES]
    assert len(names) == len(set(names)) == 14


@pytest.mark.parametrize("case", CASES, ids=[case["name"][:60] for case in CASES])
def test_vector_case_nodes(case, tmp_path):
    nodes, _ = _run(case, tmp_path)
    got = [{key: node[key] for key in VECTOR_NODE_KEYS} for node in nodes]
    assert got == case["expect"]["nodes"]


@pytest.mark.parametrize("case", CASES, ids=[case["name"][:60] for case in CASES])
def test_vector_case_pvs(case, tmp_path):
    _, pvs = _run(case, tmp_path)
    assert pvs == case["expect"]["pvs"]


@pytest.mark.parametrize("case", CASES, ids=[case["name"][:60] for case in CASES])
def test_vector_case_node_shape(case, tmp_path):
    nodes, _ = _run(case, tmp_path)
    for node in nodes:
        assert {"screen", "instance", "nodeId", "type", "props", "macros", "unresolvedMacro"} <= set(node)
        assert isinstance(node["macros"], dict)
        assert isinstance(node["unresolvedMacro"], bool)


# --- details the vectors fix, read through the extra node fields ----------------


def _case(prefix: str) -> dict:
    (case,) = [c for c in CASES if c["name"].startswith(prefix)]
    return case


def test_instance_macros_and_unresolved_flag(tmp_path):
    nodes, _ = _run(_case("macros:"), tmp_path)
    by_id = {node["nodeId"]: node for node in nodes}
    assert by_id["m1"]["macros"]["P"] == "SR:BPM"
    assert by_id["m1"]["unresolvedMacro"] is False
    # an unresolved macro stays in the name; a used empty default flags the node too
    assert by_id["m4"]["unresolvedMacro"] is True
    assert by_id["m3"]["props"] == {"pv": "AB"}
    assert by_id["m3"]["unresolvedMacro"] is True
    # the embedded instance carries its own effective macros
    assert by_id["l1"]["macros"]["DEV"] == "SR:BPM:7"
    assert by_id["l1"]["macros"]["OVR"] == "node"


def test_default_mode_is_the_viewers(tmp_path):
    """Without ``top_on_path`` the top is not on the cycle path: a self-embed expands it once more."""
    case = _case("cycle, viewer mode")
    store = _write_case(tmp_path, case)
    top = case["docs"][case["top"]]
    nodes = screens.expand(top, store, case["top"])
    assert [n["nodeId"] for n in nodes] == [n["nodeId"] for n in case["expect"]["nodes"]]


def test_dotted_embed_name_loads_by_file_path(tmp_path):
    nodes, _ = _run(_case("viewer mode (screenPath null)"), tmp_path)
    assert "nest/parts/inner.v1" in {node["screen"] for node in nodes}


# --- resolver edges beyond the vectors ---------------------------------------


def _screen(sid: str, *children, macros=None, formulas=None, version="1.0") -> dict:
    doc = {
        "schemaVersion": version,
        "kind": "screen",
        "id": sid,
        "metadata": {"title": sid, "source": {"type": "hand-authored"}, "size": {"width": 10, "height": 10}},
        "macros": macros or [],
        "root": {"id": f"{sid}-root", "type": "absolute-canvas", "props": {}, "children": list(children)},
    }
    if formulas is not None:
        doc["formulas"] = formulas
    return doc


def _w(nid: str, ntype: str, children=None, rules=None, **props) -> dict:
    node = {"id": nid, "type": ntype, "props": props}
    if children is not None:
        node["children"] = children
    if rules is not None:
        node["rules"] = rules
    return node


def _put(root: Path, key: str, doc) -> None:
    path = root / "screens" / f"{key}.screen.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(doc if isinstance(doc, str) else json.dumps(doc), encoding="utf-8")


def test_a_json_value_that_is_not_a_document_is_unloadable(tmp_path):
    _put(tmp_path, "t/list", "[1, 2]")
    _put(tmp_path, "t/null", "null")
    _put(tmp_path, "t/ok", _screen("ok", _w("o", "pv-label", pv="OK")))
    top = _screen(
        "top",
        _w("e0", "embedded-display", file="list.screen.json"),
        _w("e1", "embedded-display", file="null.screen.json"),
        _w("e2", "embedded-display", file="ok.screen.json"),
    )
    store = DraftStore(tmp_path)
    nodes = screens.expand(top, store, "t/top")
    assert {n["instance"] for n in nodes} == {"", "/2:e2"}
    assert screens.expanded_pvs(top, store, "t/top") == ["OK"]


def test_a_symlink_leaving_the_root_is_unloadable(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "x.screen.json").write_text(json.dumps(_screen("x", _w("x", "pv-label", pv="ESCAPED"))))
    root = tmp_path / "root"
    (root / "screens" / "t").mkdir(parents=True)
    (root / "screens" / "t" / "x.screen.json").symlink_to(outside / "x.screen.json")
    top = _screen("top", _w("e", "embedded-display", file="x.screen.json"))
    assert screens.expanded_pvs(top, DraftStore(root), "t/top") == []


def test_an_embed_outside_screens_is_unloadable_even_when_the_file_exists(tmp_path):
    (tmp_path / "screens").mkdir()
    (tmp_path / "outside.screen.json").write_text(json.dumps(_screen("o", _w("o", "pv-label", pv="OUT"))))
    top = _screen("top", _w("e", "embedded-display", file="../../outside.screen.json"))
    for mode in (False, True):
        assert screens.expanded_pvs(top, DraftStore(tmp_path), "t/top", top_on_path=mode) == []


def test_no_store_dir_still_expands_the_top(tmp_path):
    top = _screen("top", _w("l", "pv-label", pv="A"), _w("e", "embedded-display", file="gone.screen.json"))
    assert screens.expanded_pvs(top, DraftStore(tmp_path / "absent"), "x") == ["A"]


def test_formula_bindings_only_from_used_formulas(tmp_path):
    formulas = [
        {"name": "used", "expression": "{a} + {b}", "bindings": {"a": "${P}:A", "b": "fox://chained"}},
        {"name": "chained", "expression": "{c}", "bindings": {"c": "C"}},
        {"name": "unused", "expression": "{d}", "bindings": {"d": "D"}},
    ]
    top = _screen(
        "top",
        _w("l", "pv-label", pv="fox://used"),
        macros=[{"name": "P", "default": "SYS"}],
        formulas=formulas,
    )
    assert screens.expanded_pvs(top, DraftStore(tmp_path), "t") == ["C", "SYS:A"]


def test_macro_pattern_is_ascii_like_js_word(tmp_path):
    top = _screen("top", _w("l", "pv-label", pv="${É}:X"), _w("m", "pv-label", pv="${A_1}:Y"))
    nodes = screens.expand(top, DraftStore(tmp_path), "t", macros={"É": "no", "A_1": "yes"})
    pvs = {n["nodeId"]: n["props"]["pv"] for n in nodes if n["nodeId"] in ("l", "m")}
    # `${É}` is not a macro reference for the runtime: it stays verbatim and counts as a PV name
    assert pvs == {"l": "${É}:X", "m": "yes:Y"}
    assert screens.expanded_pvs(top, DraftStore(tmp_path), "t", macros={"É": "no", "A_1": "yes"}) == [
        "${É}:X",
        "yes:Y",
    ]


# --- statically hidden nodes (base ``visible: false``) ------------------------
#
# The runtime, the renderer and ``expand`` share one rule, pinned by the vectors'
# "hidden nodes" case: a node whose base props carry ``visible: false`` is
# neither listed nor descended into, and a hidden embed keeps its index without
# being expanded. ``include_hidden=True`` is canopy's alone (the safety
# listings), so the keys it adds are pinned here and nowhere else.


def _hidden_top() -> dict:
    return _screen(
        "top",
        _w("shown", "pv-label", pv="SHOWN"),
        _w("off", "group", visible=False, children=[_w("inner", "pv-label", pv="INNER")]),
        _w("offembed", "embedded-display", visible=False, file="leaf.screen.json"),
        _w("ruled", "pv-label", pv="RULED", visible="${V}"),
        macros=[{"name": "V", "default": "false"}],
    )


def test_hidden_nodes_are_not_listed(tmp_path):
    _put(tmp_path, "h/leaf", _screen("leaf", _w("lf", "pv-label", pv="LEAF")))
    top = _hidden_top()
    nodes = screens.expand(top, DraftStore(tmp_path), "h/top")
    # only a boolean false hides, as the renderer's `visible !== false`
    assert {n["nodeId"]: n["hidden"] for n in nodes} == {"top-root": False, "shown": False, "ruled": False}
    # a hidden node opens no subscription
    assert screens.expanded_pvs(top, DraftStore(tmp_path), "h/top") == ["RULED", "SHOWN"]


def test_include_hidden_lists_hidden_nodes_and_marks_them(tmp_path):
    _put(tmp_path, "h/leaf", _screen("leaf", _w("lf", "pv-label", pv="LEAF")))
    nodes = screens.expand(_hidden_top(), DraftStore(tmp_path), "h/top", include_hidden=True)
    assert [(n["instance"], n["nodeId"], n["hidden"]) for n in nodes] == [
        ("", "top-root", False),
        ("", "shown", False),
        ("", "off", True),
        ("", "inner", True),
        ("", "offembed", True),
        ("", "ruled", False),
        ("/0:offembed", "leaf-root", True),
        ("/0:offembed", "lf", True),
    ]


def test_include_hidden_keys_of_the_vector_case(tmp_path):
    """``[hidden embed E1, hidden container holding E2, visible E3]``: ``/0:E1``, ``/h:E2``, and E3 stays ``/1:E3``."""
    case = _case("hidden nodes:")
    store = _write_case(tmp_path, case)
    top = case["docs"][case["top"]]
    kwargs = {"macros": case["macros"], "top_on_path": True}
    shown = screens.expand(top, store, case["top"], **kwargs)
    nodes = screens.expand(top, store, case["top"], include_hidden=True, **kwargs)
    assert [(n["instance"], n["screen"], n["nodeId"], n["hidden"]) for n in nodes] == [
        ("", "hid/top", "hid-top-root", False),
        ("", "hid/top", "E1", True),
        ("", "hid/top", "HC", True),
        ("", "hid/top", "E2", True),
        ("", "hid/top", "H2", True),
        ("", "hid/top", "H1", True),
        ("", "hid/top", "E3", False),
        ("/0:E1", "hid/e1", "hid-e1-root", True),
        ("/0:E1", "hid/e1", "x1", True),
        ("/h:E2", "hid/e2", "hid-e2-root", True),
        ("/h:E2", "hid/e2", "x2", True),
        ("/1:E3", "hid/e3", "hid-e3-root", False),
        ("/1:E3", "hid/e3", "x3", False),
    ]
    # the shown nodes are the same rows, in the same order, in both walks
    assert [n for n in nodes if not n["hidden"]] == shown
    assert all(n["hidden"] is False for n in shown)


def test_a_hidden_embed_without_a_file_reserves_no_index(tmp_path):
    _put(tmp_path, "h/leaf", _screen("leaf", _w("lf", "pv-label", pv="LEAF")))
    top = _screen(
        "top",
        _w("nofile", "embedded-display", visible=False, file=""),
        _w("e", "embedded-display", file="leaf.screen.json"),
    )
    for include_hidden in (False, True):
        nodes = screens.expand(top, DraftStore(tmp_path), "h/top", include_hidden=include_hidden)
        assert {n["instance"] for n in nodes} == {"", "/0:e"}


def test_everything_inside_a_hidden_instance_is_hidden(tmp_path):
    _put(tmp_path, "h/leaf", _screen("leaf", _w("lf", "pv-label", pv="LEAF")))
    _put(tmp_path, "h/mid", _screen("mid", _w("m", "embedded-display", file="leaf.screen.json")))
    top = _screen("top", _w("e", "embedded-display", visible=False, file="mid.screen.json"))
    nodes = screens.expand(top, DraftStore(tmp_path), "h/top", include_hidden=True)
    assert [(n["instance"], n["nodeId"], n["hidden"]) for n in nodes] == [
        ("", "top-root", False),
        ("", "e", True),
        ("/0:e", "mid-root", True),
        ("/0:e", "m", True),
        ("/0:e/0:m", "leaf-root", True),
        ("/0:e/0:m", "lf", True),
    ]


# --- trees and listings ----------------------------------------------------------


def _tree(**docs) -> BufferTree:
    return BufferTree({f"screens/{key}.screen.json": json.dumps(doc).encode() for key, doc in docs.items()})


def test_a_tree_stands_in_for_a_store(tmp_path):
    leaf = _screen("leaf", _w("lf", "pv-slider", pv="LEAF"))
    top = _screen("top", _w("e", "embedded-display", file="leaf.screen.json"))
    tree = _tree(**{"t/leaf": leaf, "t/top": top, "t/dotted.v1": leaf})
    assert screens.expanded_pvs(top, tree, "t/top") == ["LEAF"]
    assert [n["instance"] for n in screens.expand(top, tree, "t/top")] == ["", "", "/0:e", "/0:e"]
    _put(tmp_path, "t/leaf", leaf)
    assert screens.expanded_pvs(top, StoreTree(DraftStore(tmp_path)), "t/top") == ["LEAF"]


def test_a_case_mismatched_embed_is_not_expanded_under_a_listing(tmp_path):
    """File ``Embedded/_X``, ref ``embedded/_X``: a case-insensitive host would open the file; a listing does not."""
    part = _screen("x", _w("s", "pv-slider", pv="PART"))
    _put(tmp_path, "a/Embedded/_X", part)
    top = _screen(
        "top",
        _w("folded", "embedded-display", file="embedded/_X.screen.json"),
        _w("exact", "embedded-display", file="Embedded/_X.screen.json"),
    )
    _put(tmp_path, "a/top", top)
    store = DraftStore(tmp_path)
    listing = frozenset({"screens/a/top.screen.json", "screens/a/Embedded/_X.screen.json"})
    nodes = screens.expand(top, store, "a/top", listing=listing)
    assert {n["instance"] for n in nodes} == {"", "/1:exact"}
    assert {n["instance"] for n in screens.expand(top, store, "a/top", listing=listing, include_hidden=True)} == {
        "",
        "/1:exact",
    }
    assert screens.expanded_pvs(top, store, "a/top", listing=listing) == ["PART"]


def test_a_listing_is_the_only_thing_asked(tmp_path):
    """A stored embed the listing leaves out is not loaded."""
    _put(tmp_path, "a/part", _screen("part", _w("s", "pv-slider", pv="PART")))
    top = _screen("top", _w("b", "pv-button", pv="GO"), _w("e", "embedded-display", file="part.screen.json"))
    _put(tmp_path, "a/top", top)
    listing = frozenset({"screens/a/top.screen.json"})
    assert screens.expanded_pvs(top, DraftStore(tmp_path), "a/top", listing=listing) == ["GO"]


def test_a_node_type_that_is_not_a_string_is_walked_as_unmapped(tmp_path):
    top = _screen("top", _w("x", ["pv-slider"], pv="LIST"), _w("y", {"t": 1}, pv="DICT"), _w("b", "pv-button", pv="GO"))
    assert screens.expanded_pvs(top, DraftStore(tmp_path), "t") == ["GO"]
    assert [n["nodeId"] for n in screens.expand(top, DraftStore(tmp_path), "t", include_hidden=True)][1:] == [
        "x",
        "y",
        "b",
    ]


def test_not_mcp_tools():
    import canopy_mcp.screens as module

    source = Path(module.__file__).read_text(encoding="utf-8")
    assert "fastmcp" not in source
    assert "mcp.tool" not in source
