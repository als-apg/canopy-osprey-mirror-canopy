"""Semantic pass: one test (or more) per code."""

from __future__ import annotations

from typing import Any
from pathlib import Path

import pytest

from canopy_mcp.check import WRITE_CAPABLE_TYPES, check_semantic
from canopy_mcp.catalog import load_prop_schemas
from canopy_mcp.check.semantic import PLOT_CURVE_KEY
from tests.canopy_mcp.synthetic_spec import write_spec


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


#: Every viewer tool page: the hints that name one are tested with all of them served.
ALL_TOOLS = ("trace", "correlation", "waveform")


def _served(doc: Any, **kwargs: Any) -> list[Any]:
    return check_semantic(doc, tools=ALL_TOOLS, **kwargs)


def _codes(doc: dict[str, Any]) -> list[str]:
    return [f.code for f in _served(doc)]


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
        {"id": "r1", "name": "n", "targetProperty": "t", "default": 0, "formula": "formula://missing_a"},
        {"id": "r2", "name": "n", "targetProperty": "t", "default": 0, "formula": "ok"},
        {
            "id": "r3",
            "name": "n",
            "targetProperty": "t",
            "default": 0,
            "pvs": [{"name": "SR:PV", "trigger": True}, {"name": "formula://missing_c", "trigger": True}],
            "conditions": [],
        },
    ]
    node = _node("w-1", props={"pvName": "formula://missing_p", "other": "formula://ok"}, rules=rules)
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


# --- write_not_relayed ----------------------------------------------------


@pytest.mark.parametrize("ntype", sorted(WRITE_CAPABLE_TYPES))
def test_write_not_relayed_on_control_types(ntype: str) -> None:
    (f,) = check_semantic(_doc(_node("w-1", ntype)))
    assert (f.code, f.severity, f.node_id, f.path) == ("write_not_relayed", "warning", "w-1", "/root/children/0/type")


def test_write_capable_type_set_is_exactly_the_eight() -> None:
    assert WRITE_CAPABLE_TYPES == {
        "pv-button",
        "pv-toggle",
        "pv-slider",
        "pv-text-input",
        "pv-checkbox",
        "pv-enum-combobox",
        "pv-spinbox",
        "pv-radio-group",
    }


def test_write_not_relayed_on_write_pv_action() -> None:
    actions = [
        {"type": "open_display", "file": "a.screen.json", "target": "replace"},
        {"type": "write_pv", "pvName": "SR:X", "value": 1},
    ]
    (f,) = check_semantic(_doc(_node("w-1", "action-button", props={"actions": actions})))
    assert (f.code, f.severity, f.node_id) == ("write_not_relayed", "warning", "w-1")
    assert f.path == "/root/children/0/props/actions/1"


# --- writes_to_machine (relay on) -----------------------------------------


def _codes_on(doc: dict[str, Any]) -> list[str]:
    return [f.code for f in check_semantic(doc, relay_on=True)]


def test_relay_flag_defaults_off() -> None:
    doc = _doc(_node("w-1", "pv-button", props={"pv": "SR:X"}))
    assert _codes(doc) == ["write_not_relayed"]
    assert [f.code for f in check_semantic(doc, relay_on=False)] == ["write_not_relayed"]


@pytest.mark.parametrize("ntype", sorted(WRITE_CAPABLE_TYPES))
def test_relay_on_control_with_a_machine_channel_writes_to_the_machine(ntype: str) -> None:
    (f,) = check_semantic(_doc(_node("w-1", ntype, props={"pv": "SR:X"})), relay_on=True)
    assert (f.code, f.severity, f.node_id, f.path) == ("writes_to_machine", "warning", "w-1", "/root/children/0/type")
    assert f.message.endswith("writes to the machine")
    assert ntype in f.message


@pytest.mark.parametrize(
    "props",
    [
        {"pv": "loc://flag"},
        {"pv": "formula://calc"},
        {"pv": "loc://a", "readbackPV": "formula://b"},
        {"pv": "  "},
        {},
    ],
)
def test_relay_on_control_without_a_machine_channel_is_not_relayed(props: dict[str, Any]) -> None:
    formulas = [{"name": "calc", "expression": "1"}, {"name": "b", "expression": "1"}]
    doc = _doc(_node("w-1", "pv-toggle", props=props), formulas=formulas)
    assert _codes_on(doc) == ["write_not_relayed"]


@pytest.mark.parametrize(
    "props",
    [
        {"pv": "loc://a", "readbackPV": "SR:RB"},
        {"pv": "ca://SR:X"},
        {"pv": "pva://SR:X"},
        {"pv": "${P}:X"},
        {"pvName": " SR:X "},
    ],
)
def test_relay_on_any_machine_channel_makes_the_control_write(props: dict[str, Any]) -> None:
    doc = _doc(_node("w-1", "pv-slider", props=props))
    assert _codes_on(doc) == ["writes_to_machine"]


def test_relay_on_write_pv_action_with_a_machine_pv_writes_to_the_machine() -> None:
    actions = [
        {"type": "write_pv", "pvName": "loc://x", "value": 1},
        {"type": "write_pv", "pvName": "SR:X", "value": 1},
        {"type": "write_pv", "pvName": "formula://f", "value": 1},
    ]
    doc = _doc(_node("w-1", "action-button", props={"actions": actions}), formulas=[{"name": "f", "expression": "1"}])
    found = [(f.code, f.path) for f in check_semantic(doc, relay_on=True)]
    assert found == [
        ("write_not_relayed", "/root/children/0/props/actions/0"),
        ("writes_to_machine", "/root/children/0/props/actions/1"),
        ("write_not_relayed", "/root/children/0/props/actions/2"),
    ]
    machine = check_semantic(doc, relay_on=True)[1]
    assert machine.severity == "warning"
    assert machine.message == "write_pv action writes to the machine"


def test_relay_off_write_pv_action_with_a_machine_pv_is_not_relayed() -> None:
    actions = [{"type": "write_pv", "pvName": "SR:X", "value": 1}]
    assert _codes(_doc(_node("w-1", "action-button", props={"actions": actions}))) == ["write_not_relayed"]


@pytest.mark.parametrize("pv_name", [None, 3, "", "   "])
def test_relay_on_write_pv_action_without_a_usable_pv_is_not_relayed(pv_name: Any) -> None:
    action: dict[str, Any] = {"type": "write_pv", "value": 1}
    if pv_name is not None:
        action["pvName"] = pv_name
    assert _codes_on(_doc(_node("w-1", "action-button", props={"actions": [action]}))) == ["write_not_relayed"]


def test_relay_on_read_only_widget_has_no_write_finding() -> None:
    assert _codes_on(_doc(_node("w-1", "pv-label", props={"pv": "SR:X"}))) == []


def test_relay_on_tolerates_malformed_props() -> None:
    node = _node("w-1", "pv-button")
    node["props"] = "nope"
    assert _codes_on(_doc(node)) == ["write_not_relayed"]
    assert check_semantic("not a doc", relay_on=True) == []


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


# --- curve_channel ---------------------------------------------------------


def _plot(ntype: str, *curves: Any) -> dict[str, Any]:
    return _doc(_node("w-p", ntype, props={"curves": list(curves)}))


@pytest.mark.parametrize(
    "curve",
    [
        {"channel": "SR:X"},
        {"pv": "SR:X"},
        {"pvName": "SR:X"},
        {"y_channels": ["SR:X"]},
        {"name": "x"},
        {"y_channel": ""},
        {"y_channel": 3},
    ],
)
def test_curve_channel_waveform_curve_without_y_channel_is_an_error(curve: dict[str, Any]) -> None:
    (f,) = _served(_plot("waveform-plot", curve))
    assert (f.code, f.severity, f.node_id) == ("curve_channel", "error", "w-p")
    assert f.path == "/root/children/0/props/curves/0/y_channel"
    assert "names no y_channel" in f.message and "Trace tab" in f.message


def test_curve_channel_names_the_key_that_was_written_instead() -> None:
    (f,) = _served(_plot("waveform-plot", {"channel": "SR:X"}))
    assert "(found 'channel' instead)" in f.message
    (g,) = _served(_plot("xy-plot", {"y_channels": ["SR:X"]}))
    assert "(found 'y_channels' instead)" in g.message and "correlation page" in g.message
    (h,) = _served(_plot("pv-array-plot", {"y_channel": "SR:X"}))
    assert "names no y_channels (found 'y_channel' instead)" in h.message


@pytest.mark.parametrize(
    "curve",
    [
        {"y_channel": "SR:X"},
        {"y_channel": "SR:X", "x_channel": None},
        {"y_channel": "${P}:WF", "x_channel": "SR:T", "name": "a", "color": "#000000", "lineStyle": 1},
    ],
)
def test_curve_channel_waveform_and_xy_curves_with_y_channel_pass(curve: dict[str, Any]) -> None:
    """No ``curve_channel``; a lone waveform plot draws only the ``waveform_tab`` hint."""
    assert _codes(_plot("waveform-plot", curve)) == ["waveform_tab"]
    assert _served(_plot("xy-plot", curve)) == []


def test_curve_channel_x_channel_must_be_a_name_or_null() -> None:
    (f,) = _served(_plot("xy-plot", {"y_channel": "SR:Y", "x_channel": ["SR:X"]}))
    assert (f.code, f.severity, f.path) == ("curve_channel", "error", "/root/children/0/props/curves/0/x_channel")


@pytest.mark.parametrize(
    "curve",
    [{"y_channel": "SR:X"}, {"y_channels": []}, {"y_channels": ["SR:X", ""]}, {"y_channels": "SR:X"}, {}],
)
def test_curve_channel_array_plot_curve_without_y_channels_is_an_error(curve: dict[str, Any]) -> None:
    (f,) = _served(_plot("pv-array-plot", curve))
    assert (f.code, f.path) == ("curve_channel", "/root/children/0/props/curves/0/y_channels")
    assert "names no y_channels" in f.message


def test_curve_channel_array_plot_curve_with_channels_passes() -> None:
    curve = {"name": "X", "color": "#1f77b4", "y_channels": ["SR:1", "SR:2"]}
    assert _served(_plot("pv-array-plot", curve)) == []


def test_curve_channel_reports_each_bad_curve_and_tolerates_malformed_input() -> None:
    findings = _served(_plot("waveform-plot", "nope", {"y_channel": "SR:A"}, {"pv": "SR:B"}))
    assert [(f.code, f.path) for f in findings] == [
        ("curve_channel", "/root/children/0/props/curves/0"),
        ("curve_channel", "/root/children/0/props/curves/2/y_channel"),
    ]
    assert _served(_doc(_node("w-p", "waveform-plot", props={"curves": "nope"}))) == []
    assert _served(_doc(_node("w-p", "waveform-plot", props={}))) == []
    assert _served(_doc(_node("w-p", "waveform-plot", props=[]))) == []
    assert _served(_doc(_node("w-l", "pv-label", props={"curves": [{"channel": "SR:X"}]}))) == []


# --- waveform_tab ----------------------------------------------------------

_WF_CURVE = {"y_channel": "SR:WF"}


def test_waveform_tab_hint_for_a_screen_that_is_a_lone_waveform_plot() -> None:
    (f,) = _served(_plot("waveform-plot", _WF_CURVE))
    assert (f.code, f.severity, f.node_id, f.path) == ("waveform_tab", "warning", "w-p", "/root/children/0/type")
    assert "Waveform tab" in f.message and "waveform-plot" in f.message


def test_waveform_tab_hint_tolerates_titles_frames_and_overlaid_curves() -> None:
    """Labels, boxes and containers are not controls; two overlaid curves are still the tab's job."""
    plot = _node("w-p", "waveform-plot", props={"curves": [_WF_CURVE, {"y_channel": "SR:REF"}]})
    doc = _doc(_node("w-t", "text-label", props={"text": "Orbit"}), _node("w-b", "group-box", [plot]))
    findings = _served(doc)
    assert [(f.code, f.node_id, f.path) for f in findings] == [
        ("waveform_tab", "w-p", "/root/children/1/children/0/type")
    ]
    canvas = _doc()
    canvas["root"] = _node("w-root", "absolute-canvas", [_node("w-p", "waveform-plot", props={"curves": [_WF_CURVE]})])
    assert _codes(canvas) == ["waveform_tab"]


def test_waveform_tab_hint_is_raised_once_for_several_lone_waveform_plots() -> None:
    a = _node("w-a", "waveform-plot", props={"curves": [_WF_CURVE]})
    b = _node("w-b", "waveform-plot", props={"curves": [{"y_channel": "SR:WF2"}]})
    (f,) = _served(_doc(a, b))
    assert (f.code, f.node_id) == ("waveform_tab", "w-a")


@pytest.mark.parametrize(
    "other",
    [
        _node("w-l", "pv-label", props={"pv": "SR:CUR"}),
        _node("w-s", "pv-slider", props={"pv": "SR:SET"}),
        _node("w-x", "xy-plot", props={"curves": [{"y_channel": "SR:Y", "x_channel": "SR:X"}]}),
        _node("w-e", "embedded-display", props={"file": "other.screen.json"}),
        _node("w-u", "unknown-widget", props={}),
    ],
)
def test_no_waveform_tab_hint_for_a_waveform_plot_beside_other_controls(other: dict[str, Any]) -> None:
    doc = _doc(_node("w-p", "waveform-plot", props={"curves": [_WF_CURVE]}), other)
    assert "waveform_tab" not in _codes(doc)


def test_no_waveform_tab_hint_when_a_control_is_nested_deeper() -> None:
    plot = _node("w-p", "waveform-plot", props={"curves": [_WF_CURVE]})
    doc = _doc(plot, _node("w-g", "group", [_node("w-btn", "pv-button", props={"pv": "SR:CMD"})]))
    assert "waveform_tab" not in _codes(doc)


@pytest.mark.parametrize(
    "props",
    [{}, [], {"curves": []}, {"curves": "nope"}, {"curves": [{"channel": "SR:WF"}]}, {"curves": [_WF_CURVE, "x"]}],
)
def test_no_waveform_tab_hint_for_a_plot_that_draws_nothing_or_has_a_refused_curve(props: Any) -> None:
    """A broken plot gets its ``curve_channel`` error alone; the hint waits until the plot draws."""
    assert "waveform_tab" not in _codes(_doc(_node("w-p", "waveform-plot", props=props)))


def test_no_waveform_tab_hint_for_a_screen_without_a_waveform_plot() -> None:
    assert _served(_doc(_node("w-t", "text-label", props={"text": "x"}))) == []
    assert _served(_plot("xy-plot", {"y_channel": "SR:Y", "x_channel": "SR:X"})) == []
    assert _served({"root": None}) == []


def test_hints_name_no_tool_page_the_host_does_not_serve() -> None:
    """Without the tool pages, a lone waveform plot is a screen and the curve hints name no tab."""
    assert check_semantic(_plot("waveform-plot", _WF_CURVE)) == []
    (f,) = check_semantic(_plot("waveform-plot", {"channel": "SR:X"}))
    assert "names no y_channel" in f.message and "Trace" not in f.message and "tab" not in f.message
    (g,) = check_semantic(_plot("xy-plot", {"pv": "SR:X"}), tools=("trace",))
    assert "correlation" not in g.message
    assert check_semantic(_plot("waveform-plot", _WF_CURVE), tools=("trace", "correlation")) == []


@pytest.mark.parametrize("ntype", [["pv-button"], {"t": "pv-slider"}, 3, None])
def test_a_node_type_that_is_not_a_string_raises_nothing(ntype: Any) -> None:
    doc = _doc(_node("w-p", "waveform-plot", props={"curves": [_WF_CURVE]}), _node("w-x", ntype))
    assert "waveform_tab" not in _codes(doc)
    assert check_semantic(_doc(_node("w-x", ntype, props={"pv": "SR:X"})), relay_on=True) == []


def test_curve_key_per_plot_type_is_the_one_key_the_staged_schema_requires(tmp_path: Path) -> None:
    """``PLOT_CURVE_KEY`` stands in for the staged prop schemas: same plot types, same single required key."""
    schemas = load_prop_schemas(write_spec(tmp_path / "spec"))
    required = {
        widget_type: schema["properties"]["curves"]["items"]["required"]
        for widget_type, schema in schemas.items()
        if "curves" in schema.get("properties", {})
    }
    assert required == {widget_type: [key] for widget_type, key in PLOT_CURVE_KEY.items()}
