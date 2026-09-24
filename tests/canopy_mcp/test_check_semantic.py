"""Semantic pass: one test (or more) per code."""

from __future__ import annotations

from typing import Any

import pytest

from canopy_mcp.check import INERT_IN_OSPREY_TYPES, check_semantic


def _node(nid: str, ntype: str = "label", children: list[dict[str, Any]] | None = None, **extra: Any) -> dict[str, Any]:
    node: dict[str, Any] = {
        "id": nid,
        "type": ntype,
        "props": {},
        "geometry": {"x": 0, "y": 0, "width": 10, "height": 10},
    }
    if children is not None:
        node["children"] = children
    node.update(extra)
    return node


def _doc(*children: dict[str, Any], **top: Any) -> dict[str, Any]:
    doc: dict[str, Any] = {
        "schemaVersion": "1.0",
        "kind": "screen",
        "id": "demo",
        "metadata": {"title": "D", "source": {"type": "native"}, "size": {"width": 1, "height": 1}},
        "macros": [],
        "root": _node("w-root", "group", list(children)),
    }
    doc.update(top)
    return doc


def _codes(doc: dict[str, Any]) -> list[str]:
    return [f.code for f in check_semantic(doc)]


def test_clean_document_has_no_findings() -> None:
    assert check_semantic(_doc(_node("w-1"))) == []


def test_malformed_input_is_tolerated() -> None:
    assert check_semantic("nope") == []
    assert check_semantic({"root": 3, "macros": "x", "formulas": 5}) == []
    doc = _doc({"id": "w-x", "props": "bad", "children": "bad", "rules": [1], "warnings": 7})
    assert check_semantic(doc) == []


# --- macro_default ---------------------------------------------------------


def test_macro_default_screen_macro_without_default() -> None:
    doc = _doc(macros=[{"name": "P", "default": "X"}, {"name": "Q"}])
    (f,) = check_semantic(doc)
    assert (f.code, f.severity, f.path, f.node_id) == ("macro_default", "error", "/macros/1", None)
    assert "Q" in f.message


def test_macro_default_template_macro_with_default() -> None:
    doc = _doc(kind="template", macros=[{"name": "P", "default": "X"}, {"name": "Q"}])
    (f,) = check_semantic(doc)
    assert (f.code, f.path) == ("macro_default", "/macros/0")


def test_macro_default_null_counts_as_missing() -> None:
    doc = _doc(macros=[{"name": "P", "default": None}])
    assert _codes(doc) == ["macro_default"]


# --- unresolved_formula ----------------------------------------------------


def test_unresolved_formula_in_prop_rule_formula_and_rule_pv() -> None:
    rules = [
        {"id": "r1", "name": "n", "targetProperty": "t", "default": 0, "formula": "fox://missing_a"},
        {"id": "r2", "name": "n", "targetProperty": "t", "default": 0, "formula": "ok"},
        {
            "id": "r3",
            "name": "n",
            "targetProperty": "t",
            "default": 0,
            "pvs": [{"name": "SR:PV", "trigger": True}, {"name": "fox://missing_c", "trigger": True}],
            "conditions": [],
        },
    ]
    node = _node("w-1", props={"pvName": "fox://missing_p", "other": "fox://ok"}, rules=rules)
    doc = _doc(node, formulas=[{"name": "ok", "expression": "1"}])
    findings = check_semantic(doc)
    assert all(f.code == "unresolved_formula" and f.severity == "error" for f in findings)
    assert all(f.node_id == "w-1" for f in findings)
    assert sorted(f.path for f in findings) == [
        "/root/children/0/props/pvName",
        "/root/children/0/rules/0/formula",
        "/root/children/0/rules/2/pvs/1/name",
    ]


def test_bare_rule_formula_name_must_resolve() -> None:
    rule = {"id": "r", "name": "n", "targetProperty": "t", "default": 0, "formula": "nope"}
    assert _codes(_doc(_node("w-1", rules=[rule]))) == ["unresolved_formula"]


# --- duplicate_node_id -----------------------------------------------------


def test_duplicate_node_id_reported_on_the_repeat() -> None:
    doc = _doc(_node("w-1", children=[_node("w-2")]), _node("w-2"))
    (f,) = check_semantic(doc)
    assert (f.code, f.severity, f.node_id) == ("duplicate_node_id", "error", "w-2")
    assert f.path == "/root/children/1/id"
    assert "/root/children/0/children/0" in f.message


# --- open_webpage ----------------------------------------------------------


@pytest.mark.parametrize(
    "url", ["javascript:alert(1)", " JavaScript:alert(1)", "data:text/html,x", "file:///etc/passwd", "foo.html", ""]
)
def test_open_webpage_non_http_url_is_error(url: str) -> None:
    node = _node("w-9", "action-button", props={"actions": [{"type": "open_webpage", "url": url}]})
    (f,) = check_semantic(_doc(node))
    assert (f.code, f.severity, f.node_id) == ("open_webpage", "error", "w-9")
    assert f.path == "/root/children/0/props/actions/0/url"


@pytest.mark.parametrize("url", ["http://x.org", "HTTPS://x.org/a?b", "/docs/page.html"])
def test_open_webpage_http_and_root_relative_are_fine(url: str) -> None:
    node = _node("w-9", "action-button", props={"actions": [{"type": "open_webpage", "url": url}]})
    assert check_semantic(_doc(node)) == []


# --- inert_in_osprey -------------------------------------------------------


@pytest.mark.parametrize("ntype", sorted(INERT_IN_OSPREY_TYPES))
def test_inert_in_osprey_on_control_types(ntype: str) -> None:
    (f,) = check_semantic(_doc(_node("w-1", ntype)))
    assert (f.code, f.severity, f.node_id, f.path) == ("inert_in_osprey", "warning", "w-1", "/root/children/0/type")


def test_inert_type_set_is_exactly_the_eight() -> None:
    assert INERT_IN_OSPREY_TYPES == {
        "pv-button",
        "pv-toggle",
        "pv-slider",
        "pv-text-input",
        "pv-checkbox",
        "pv-enum-combobox",
        "pv-spinbox",
        "pv-radio-group",
    }


def test_inert_in_osprey_on_write_pv_action() -> None:
    actions = [
        {"type": "open_display", "file": "a.screen.json", "target": "replace"},
        {"type": "write_pv", "pvName": "SR:X", "value": 1},
    ]
    (f,) = check_semantic(_doc(_node("w-1", "action-button", props={"actions": actions})))
    assert (f.code, f.severity, f.node_id) == ("inert_in_osprey", "warning", "w-1")
    assert f.path == "/root/children/0/props/actions/1"


# --- warning ---------------------------------------------------------------


def test_node_warnings_surface_as_warning() -> None:
    node = _node("w-4", warnings=["script needs the script runtime", "on/off images not carried"])
    findings = check_semantic(_doc(node))
    assert [(f.code, f.severity, f.node_id, f.path, f.message) for f in findings] == [
        ("warning", "warning", "w-4", "/root/children/0/warnings/0", "script needs the script runtime"),
        ("warning", "warning", "w-4", "/root/children/0/warnings/1", "on/off images not carried"),
    ]


# --- unknown_widget --------------------------------------------------------


def test_unknown_widget_carries_original_class() -> None:
    node = _node("w-7", "unknown-widget", props={"originalClass": "PyDMScatterPlot"})
    (f,) = check_semantic(_doc(node))
    assert (f.code, f.severity, f.node_id, f.path) == ("unknown_widget", "warning", "w-7", "/root/children/0/type")
    assert f.original_class == "PyDMScatterPlot"
    assert f.to_dict()["originalClass"] == "PyDMScatterPlot"


def test_unknown_widget_on_root_node() -> None:
    doc = _doc()
    doc["root"] = _node("w-root", "unknown-widget", props={})
    (f,) = check_semantic(doc)
    assert f.path == "/root/type" and f.original_class is None
