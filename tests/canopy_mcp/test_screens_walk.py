"""The screen walk: ``canopy_mcp.screens`` against the shared expand() vectors.

``fixtures/expand-vectors.json`` is a byte copy of the sdk's
``packages/screen-ir/test-vectors/expand.json``, which the runtime generates from
its ``expandScreen``. Every case is run here with the same harness rules: each
document is written to ``screens/<key>.screen.json`` of a real store, ``docsRaw``
texts are written verbatim, and ``screenPath`` a key means ``top_on_path=True``.
"""

import json
from pathlib import Path

import pytest

from canopy_mcp import screens
from canopy_mcp.store import DraftStore
from canopy_mcp.check.semantic import INERT_IN_OSPREY_TYPES

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
    assert sorted(screens.MAPPED_TYPES) == VECTORS["mappedTypes"]
    assert "embedded-display" not in screens.MAPPED_TYPES
    assert "absolute-canvas" not in screens.MAPPED_TYPES


def test_every_case_has_a_unique_name():
    names = [case["name"] for case in CASES]
    assert len(names) == len(set(names)) == 13


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
# The runtime's walk lists a node whose base props carry ``visible: false``,
# though the renderer never mounts it (nor its children, nor its embed). The
# vectors follow the runtime, so ``expand`` lists such nodes too; ``hidden``
# marks them so a walker comparing against what is on screen never expects
# them (no false absence).


def test_hidden_nodes_are_listed_and_marked(tmp_path):
    _put(tmp_path, "h/leaf", _screen("leaf", _w("lf", "pv-label", pv="LEAF")))
    top = _screen(
        "top",
        _w("shown", "pv-label", pv="SHOWN"),
        _w("off", "group", visible=False, children=[_w("inner", "pv-label", pv="INNER")]),
        _w("offembed", "embedded-display", visible=False, file="leaf.screen.json"),
        _w("ruled", "pv-label", pv="RULED", visible="${V}"),
        macros=[{"name": "V", "default": "false"}],
    )
    nodes = screens.expand(top, DraftStore(tmp_path), "h/top")
    hidden = {n["nodeId"]: n["hidden"] for n in nodes}
    assert hidden == {
        "top-root": False,
        "shown": False,
        "off": True,
        "inner": True,
        "offembed": True,
        "ruled": False,  # only a boolean false hides, as the renderer's `visible !== false`
        "leaf-root": True,
        "lf": True,
    }
    # the PV list follows the runtime's collector: hidden nodes' channels stay listed
    assert screens.expanded_pvs(top, DraftStore(tmp_path), "h/top") == ["INNER", "LEAF", "RULED", "SHOWN"]


# --- write bindings -------------------------------------------------------------


def test_list_write_bindings(tmp_path):
    _put(
        tmp_path,
        "w/leaf",
        _screen(
            "leaf",
            _w("sp", "pv-slider", pv="${DEV}:SP"),
            _w("lbl", "pv-label", pv="${DEV}:RB"),
            macros=[{"name": "DEV", "default": "D0"}],
        ),
    )
    _put(
        tmp_path,
        "w/top",
        _screen(
            "top",
            _w("btn", "pv-button", pv="${P}:GO"),
            _w(
                "act",
                "regular-button",
                actions=[
                    {"type": "write_pv", "pvName": "$(P):RESET", "value": 1},
                    {"type": "open_display", "file": "leaf.screen.json"},
                    {"type": "write_pv", "pvName": "loc://flag", "value": 1},
                ],
            ),
            _w("ti", "pv-text-input", pv="${E}X"),
            _w("un", "pv-toggle", pv="${NOPE}:T"),
            _w("e", "embedded-display", file="leaf.screen.json", macros={"DEV": "D7"}),
            macros=[{"name": "P", "default": "SR"}, {"name": "E", "default": ""}],
        ),
    )
    _put(tmp_path, "w/dotted.v1", _screen("dotted", _w("x", "pv-slider", pv="NOT:LISTED")))
    rows = screens.list_write_bindings(DraftStore(tmp_path))
    assert rows == [
        {
            "screen": "w/leaf",
            "instance": "",
            "nodeId": "sp",
            "kind": "pv-slider",
            "pvAsWritten": "${DEV}:SP",
            "pvResolved": "D0:SP",
            "local": False,
            "unresolvedMacro": False,
        },
        {
            "screen": "w/top",
            "instance": "",
            "nodeId": "btn",
            "kind": "pv-button",
            "pvAsWritten": "${P}:GO",
            "pvResolved": "SR:GO",
            "local": False,
            "unresolvedMacro": False,
        },
        {
            "screen": "w/top",
            "instance": "",
            "nodeId": "act",
            "kind": "write_pv",
            "pvAsWritten": "$(P):RESET",
            "pvResolved": "SR:RESET",
            "local": False,
            "unresolvedMacro": False,
        },
        {
            "screen": "w/top",
            "instance": "",
            "nodeId": "act",
            "kind": "write_pv",
            "pvAsWritten": "loc://flag",
            "pvResolved": "loc://flag",
            "local": True,
            "unresolvedMacro": False,
        },
        {
            "screen": "w/top",
            "instance": "",
            "nodeId": "ti",
            "kind": "pv-text-input",
            "pvAsWritten": "${E}X",
            "pvResolved": "X",
            "local": False,
            "unresolvedMacro": True,
        },
        {
            "screen": "w/top",
            "instance": "",
            "nodeId": "un",
            "kind": "pv-toggle",
            "pvAsWritten": "${NOPE}:T",
            "pvResolved": "${NOPE}:T",
            "local": False,
            "unresolvedMacro": True,
        },
        {
            "screen": "w/leaf",
            "instance": "/0:e",
            "nodeId": "sp",
            "kind": "pv-slider",
            "pvAsWritten": "${DEV}:SP",
            "pvResolved": "D7:SP",
            "local": False,
            "unresolvedMacro": False,
        },
    ]
    assert {row["kind"] for row in rows} <= INERT_IN_OSPREY_TYPES | {"write_pv"}


def test_list_write_bindings_skips_unreadable_drafts(tmp_path):
    _put(tmp_path, "a/bad", "{ not json")
    _put(tmp_path, "a/ok", _screen("ok", _w("b", "pv-button", pv="GO")))
    rows = screens.list_write_bindings(DraftStore(tmp_path))
    assert [(r["screen"], r["pvResolved"]) for r in rows] == [("a/ok", "GO")]


def test_an_empty_default_passed_down_still_flags(tmp_path):
    _put(tmp_path, "d/leaf", _screen("leaf", _w("s", "pv-slider", pv="${E}:SP")))
    _put(
        tmp_path,
        "d/top",
        _screen(
            "top",
            _w("e", "embedded-display", file="leaf.screen.json"),
            macros=[{"name": "E", "default": ""}],
        ),
    )
    rows = [r for r in screens.list_write_bindings(DraftStore(tmp_path)) if r["instance"] == "/0:e"]
    assert [(r["pvResolved"], r["unresolvedMacro"]) for r in rows] == [(":SP", True)]


def test_not_mcp_tools():
    import canopy_mcp.screens as module

    source = Path(module.__file__).read_text(encoding="utf-8")
    assert "fastmcp" not in source
    assert "mcp.tool" not in source
