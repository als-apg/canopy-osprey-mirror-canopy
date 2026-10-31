"""The write-binding listing: every PV a stored screen can write, on top of the screen walk."""

import json
from pathlib import Path

import pytest

from canopy_mcp import library_screens
from canopy_mcp.store import DraftStore
from canopy_mcp.check.refs import BufferTree
from canopy_mcp.check.semantic import WRITE_CAPABLE_TYPES


def _screen(sid: str, *children, macros=None) -> dict:
    return {
        "schemaVersion": "1.0",
        "kind": "screen",
        "id": sid,
        "metadata": {"title": sid, "source": {"type": "hand-authored"}, "size": {"width": 10, "height": 10}},
        "macros": macros or [],
        "root": {"id": f"{sid}-root", "type": "absolute-canvas", "props": {}, "children": list(children)},
    }


def _w(nid: str, ntype, children=None, rules=None, **props) -> dict:
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


def _tree(**docs) -> BufferTree:
    return BufferTree({f"screens/{key}.screen.json": json.dumps(doc).encode() for key, doc in docs.items()})


def _rows(tmp_path, *children, macros=None):
    _put(tmp_path, "s/top", _screen("top", *children, macros=macros))
    return library_screens.list_screen_write_bindings(DraftStore(tmp_path), "s/top")


# --- trees and listings ----------------------------------------------------------


def test_the_top_level_screens_of_a_tree_are_its_paths_that_name_a_draft_id():
    leaf = _screen("leaf", _w("lf", "pv-slider", pv="LEAF"))
    top = _screen("top", _w("e", "embedded-display", file="leaf.screen.json"))
    tree = _tree(**{"t/leaf": leaf, "t/top": top, "t/dotted.v1": leaf})
    assert [(r["top"], r["instance"]) for r in library_screens.list_write_bindings(tree)] == [
        ("t/leaf", ""),
        ("t/top", "/0:e"),
    ]


def test_a_case_mismatched_embed_is_not_listed_under_a_listing(tmp_path):
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
    for rows in (
        library_screens.list_screen_write_bindings(store, "a/top", listing=listing),
        [r for r in library_screens.list_write_bindings(store, listing=listing) if r["top"] == "a/top"],
    ):
        assert [(r["instance"], r["pvResolved"]) for r in rows] == [("/1:exact", "PART")]


def test_a_listing_leaves_out_embeds_and_top_level_screens(tmp_path):
    _put(tmp_path, "a/part", _screen("part", _w("s", "pv-slider", pv="PART")))
    top = _screen("top", _w("b", "pv-button", pv="GO"), _w("e", "embedded-display", file="part.screen.json"))
    _put(tmp_path, "a/top", top)
    store = DraftStore(tmp_path)
    listing = frozenset({"screens/a/top.screen.json"})
    assert [(r["top"], r["pvResolved"]) for r in library_screens.list_write_bindings(store, listing=listing)] == [
        ("a/top", "GO")
    ]
    assert library_screens.list_screen_write_bindings(store, "a/part", listing=listing) == []


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
    rows = library_screens.list_write_bindings(DraftStore(tmp_path))
    assert rows == [
        {
            "top": "w/leaf",
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
            "top": "w/top",
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
            "top": "w/top",
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
            "top": "w/top",
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
            "top": "w/top",
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
            "top": "w/top",
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
            "top": "w/top",
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
    assert {row["kind"] for row in rows} <= WRITE_CAPABLE_TYPES | {"write_pv"}
    # the listing of a store is the listing of each of its top-level screens, in manifest order
    store = DraftStore(tmp_path)
    assert rows == [
        *library_screens.list_screen_write_bindings(store, "w/leaf"),
        *library_screens.list_screen_write_bindings(store, "w/top"),
    ]
    assert library_screens.list_screen_write_bindings(store, "w/absent") == []


def test_equal_write_binding_rows_are_listed_once(tmp_path):
    _put(
        tmp_path,
        "d/top",
        _screen(
            "top",
            # one PV under two channel keys of one widget, and one written twice by one button
            _w("b", "pv-button", pv="SAME", channel="SAME", readbackPV="OTHER"),
            _w(
                "act",
                "regular-button",
                actions=[
                    {"type": "write_pv", "pvName": "TWICE", "value": 0},
                    {"type": "write_pv", "pvName": "TWICE", "value": 1},
                    {"type": "write_pv", "pvName": " TWICE", "value": 2},
                ],
            ),
        ),
    )
    rows = library_screens.list_write_bindings(DraftStore(tmp_path))
    assert [(r["top"], r["nodeId"], r["kind"], r["pvAsWritten"]) for r in rows] == [
        ("d/top", "b", "pv-button", "SAME"),
        ("d/top", "b", "pv-button", "OTHER"),
        ("d/top", "act", "write_pv", "TWICE"),
        ("d/top", "act", "write_pv", " TWICE"),  # spelled differently: its own row
    ]
    assert len({json.dumps(r, sort_keys=True) for r in rows}) == len(rows)


def test_write_bindings_under_hidden_nodes_are_listed(tmp_path):
    """A rule can show a statically hidden widget, so the safety listing walks with ``include_hidden=True``."""
    _put(tmp_path, "h/leaf", _screen("leaf", _w("sp", "pv-slider", pv="LEAF:SP")))
    _put(
        tmp_path,
        "h/top",
        _screen(
            "top",
            _w("E1", "embedded-display", visible=False, file="leaf.screen.json"),
            _w(
                "HC",
                "group",
                visible=False,
                children=[_w("E2", "embedded-display", file="leaf.screen.json"), _w("hb", "pv-button", pv="HID:GO")],
            ),
            _w("E3", "embedded-display", file="leaf.screen.json"),
        ),
    )
    rows = library_screens.list_screen_write_bindings(DraftStore(tmp_path), "h/top")
    assert [(r["top"], r["screen"], r["instance"], r["nodeId"], r["pvResolved"]) for r in rows] == [
        ("h/top", "h/top", "", "hb", "HID:GO"),
        ("h/top", "h/leaf", "/0:E1", "sp", "LEAF:SP"),
        ("h/top", "h/leaf", "/h:E2", "sp", "LEAF:SP"),
        ("h/top", "h/leaf", "/1:E3", "sp", "LEAF:SP"),
    ]


def test_list_write_bindings_skips_unreadable_drafts(tmp_path):
    _put(tmp_path, "a/bad", "{ not json")
    _put(tmp_path, "a/ok", _screen("ok", _w("b", "pv-button", pv="GO")))
    rows = library_screens.list_write_bindings(DraftStore(tmp_path))
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
    rows = [r for r in library_screens.list_write_bindings(DraftStore(tmp_path)) if r["instance"] == "/0:e"]
    assert [(r["pvResolved"], r["unresolvedMacro"]) for r in rows] == [(":SP", True)]
