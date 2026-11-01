"""Lint: catalog prop validation, in-process Fox parse, Raccoon PV lookup."""

import json
from pathlib import Path

import httpx
import pytest

from canopy_mcp import fox
from canopy_mcp.client import make_client
from canopy_mcp.config import Settings
from canopy_mcp.catalog import build_catalog
from canopy_mcp.channels import WRITE_CAPABLE_TYPES
from canopy_mcp.check.lint import (
    RACCOON_BATCH,
    RACCOON_TIMEOUT_S,
    RACCOON_VALIDATE_PATH,
    check_lint,
    collect_pvs,
    pv_findings,
    normalize_pv,
    role_findings,
)
from canopy.core.color_scheme import THEME_FILE_ENV, clear_color_scheme_cache

BASE = "http://canopy.test"
URL = BASE + RACCOON_VALIDATE_PATH
IR_MAP_FIXTURE = Path(__file__).parent / "fixtures" / "ir-map.json"
EXPAND_VECTORS = Path(__file__).parent / "fixtures" / "expand-vectors.json"

LABEL_SCHEMA = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "type": "object",
    "properties": {"pv": {"type": "string"}, "precision": {"type": "integer", "minimum": 0}},
    "required": ["pv"],
}


def _catalog():
    ir_map = {
        "pv-label": {"consumes": ["pv", "precision"], "verbatim": False},
        "pv-gauge": {"consumes": [], "verbatim": True},
        "frame": {"consumes": [], "verbatim": False},
        "waveform-plot": {"consumes": ["curves"], "verbatim": False},
    }
    return build_catalog(ir_map, {"pv-label": LABEL_SCHEMA})


def _node(nid, ntype, children=None, **extra):
    node = {"id": nid, "type": ntype, "geometry": {"x": 0, "y": 0, "width": 10, "height": 10}}
    if children is not None:
        node["children"] = children
    node.update(extra)
    return node


def _doc(*children, **top):
    doc = {"schemaVersion": "1.0", "kind": "screen", "id": "t", "root": _node("root", "frame", list(children))}
    doc.update(top)
    return doc


@pytest.fixture
def client():
    return make_client(Settings(base_url=BASE))


def _codes(findings):
    return [(f.node_id, f.code, f.severity) for f in findings]


# --- props ------------------------------------------------------------------


async def test_valid_props_no_findings(httpx_mock, client):
    httpx_mock.add_response(url=URL, method="POST", json={"known": ["A:B"], "unknown": []})
    result = await check_lint(
        _doc(_node("l", "pv-label", props={"pv": "A:B", "precision": 2})), _catalog(), client=client
    )
    assert result.findings == []
    assert result.skipped == []


async def test_undocumented_type_warns():
    result = await check_lint(_doc(_node("g", "pv-gauge", props={})), _catalog(), client=None)
    assert _codes(result.findings) == [("g", "undocumented_type", "warning")]
    assert result.findings[0].path == "/root/children/0/type"


async def test_unknown_type_errors():
    result = await check_lint(_doc(_node("x", "pv-nonsense", props={})), _catalog())
    assert _codes(result.findings) == [("x", "unknown_type", "error")]


async def test_prop_violation_path_names_prop(httpx_mock, client):
    httpx_mock.add_response(url=URL, method="POST", json={"known": ["A:B"], "unknown": []})
    doc = _doc(_node("l", "pv-label", props={"pv": "A:B", "precision": -1}))
    result = await check_lint(doc, _catalog(), client=client)
    assert _codes(result.findings) == [("l", "invalid_prop", "error")]
    assert result.findings[0].path == "/root/children/0/props/precision"


async def test_missing_required_prop_path_names_prop():
    result = await check_lint(_doc(_node("l", "pv-label", props={})), _catalog())
    assert _codes(result.findings) == [("l", "invalid_prop", "error")]
    assert result.findings[0].path == "/root/children/0/props/pv"


async def test_placeholder_yields_no_lint_finding():
    node = _node("u", "unknown-widget", props={"originalClass": "caQtFoo", "anything": 1})
    result = await check_lint(_doc(node), _catalog())
    assert result.findings == []
    assert result.skipped == []


# --- formulas ---------------------------------------------------------------


async def test_valid_formula_no_finding(httpx_mock, client):
    httpx_mock.add_response(url=URL, method="POST", json={"known": ["A:B"], "unknown": []})
    doc = _doc(formulas=[{"name": "f", "kind": "scalar", "expression": "{a} * 2", "bindings": {"a": "A:B"}}])
    result = await check_lint(doc, _catalog(), client=client)
    assert result.findings == []


async def test_invalid_formula_errors():
    doc = _doc(formulas=[{"name": "f", "expression": "{a} +* (", "bindings": {"a": "loc://x"}}])
    result = await check_lint(doc, _catalog())  # loc:// binding: no raccoon call
    assert _codes(result.findings) == [(None, "invalid_formula", "error")]
    assert result.findings[0].path == "/formulas/0/expression"


async def test_fox_validator_is_offline():
    from canopy.fox.gateway import make_offline_validator
    from canopy.fox.services.formula_service import FormulaService

    svc = make_offline_validator()
    assert isinstance(svc, FormulaService)
    assert await fox.formula_errors("{a} + 1", {"a": "A:B"}) == []
    assert await fox.formula_errors("{missing} + 1") != []


# --- pvs: collection and normalisation -------------------------------------


def test_normalize_pv():
    assert normalize_pv("ca://A:B") == "A:B"
    assert normalize_pv("pva://A:B") == "A:B"
    assert normalize_pv("loc://x") is None
    assert normalize_pv("formula://f") is None
    assert normalize_pv("${P}:X") is None
    assert normalize_pv("A:B") == "A:B"


def test_collect_pvs_sources():
    doc = _doc(
        _node(
            "w",
            "waveform-plot",
            props={"curves": [{"y_channel": "Y", "x_channel": "X"}]},
        ),
        _node(
            "l",
            "pv-label",
            props={"pv": "P", "readbackPV": "R", "actions": [{"type": "write_pv", "pvName": "W", "value": 1}]},
            rules=[
                {
                    "id": "r",
                    "name": "r",
                    "targetProperty": "x",
                    "default": 0,
                    "pvs": [{"name": "Q", "trigger": True}],
                    "conditions": [],
                }
            ],
        ),
        formulas=[{"name": "f", "expression": "{a}", "bindings": {"a": "B"}}],
    )
    assert sorted(r.name for r in collect_pvs(doc)) == ["B", "P", "Q", "R", "W", "X", "Y"]


def test_collect_pvs_reads_y_channels_item_by_item():
    curves = [
        {"name": "x", "y_channels": ["BPM:1:X", "BPM:2:X"]},
        {"name": "y", "y_channels": ["BPM:1:Y", 7, None, "loc://k"], "y_channel": "SINGLE"},
        {"name": "bad", "y_channels": "NOT:A:LIST"},
    ]
    doc = _doc(_node("a", "pv-array-plot", props={"curves": curves}))
    refs = collect_pvs(doc)
    assert [(r.name, r.node_id, r.path) for r in refs] == [
        ("BPM:1:X", "a", ("root", "children", 0, "props", "curves", 0, "y_channels", 0)),
        ("BPM:2:X", "a", ("root", "children", 0, "props", "curves", 0, "y_channels", 1)),
        ("BPM:1:Y", "a", ("root", "children", 0, "props", "curves", 1, "y_channels", 0)),
        ("loc://k", "a", ("root", "children", 0, "props", "curves", 1, "y_channels", 3)),
        ("SINGLE", "a", ("root", "children", 0, "props", "curves", 1, "y_channel")),
        ("NOT:A:LIST", "a", ("root", "children", 0, "props", "curves", 2, "y_channels")),
    ]


def test_collect_pvs_reads_any_channel_prop():
    doc = _doc(_node("s", "pv-slider", props={"controlPV": "SR:CTRL", "limits": {"lowChannel": "SR:LO"}}))
    assert [(r.name, r.node_id, r.path) for r in collect_pvs(doc)] == [
        ("SR:CTRL", "s", ("root", "children", 0, "props", "controlPV")),
        ("SR:LO", "s", ("root", "children", 0, "props", "limits", "lowChannel")),
    ]


async def test_any_channel_prop_reaches_raccoon(httpx_mock, client):
    httpx_mock.add_response(url=URL, method="POST", json={"known": ["SR:LO"], "unknown": ["SR:CTRL"]})
    doc = _doc(_node("s", "pv-slider", props={"controlPV": "SR:CTRL", "limits": {"lowChannel": "SR:LO"}}))
    findings, skipped = await pv_findings(collect_pvs(doc), client=client)
    sent = json.loads(httpx_mock.get_requests()[0].content)
    assert "SR:CTRL" in sent["pvs"]
    assert [(f.node_id, f.code, f.path) for f in findings] == [("s", "unknown_pv", "/root/children/0/props/controlPV")]
    assert skipped == []


def test_collect_pvs_asks_what_the_viewer_mounts():
    vectors = json.loads(EXPAND_VECTORS.read_bytes())["cases"]
    (case,) = [c for c in vectors if c["name"].startswith("node set = what the renderer mounts")]
    assert {r.name for r in collect_pvs(case["docs"]["um/top"])} == {"UNDER:GROUP", "UNMAPPED:RULE"}


async def test_y_channels_reach_raccoon(httpx_mock, client):
    httpx_mock.add_response(url=URL, method="POST", json={"known": ["A:1"], "unknown": ["A:2"]})
    doc = _doc(_node("a", "pv-array-plot", props={"curves": [{"name": "c", "y_channels": ["A:1", "A:2"]}]}))
    findings, skipped = await pv_findings(collect_pvs(doc), client=client)
    sent = json.loads(httpx_mock.get_requests()[0].content)
    assert sorted(sent["pvs"]) == ["A:1", "A:2"]
    assert [(f.node_id, f.path) for f in findings] == [("a", "/root/children/0/props/curves/0/y_channels/1")]
    assert skipped == []


# --- pvs: raccoon transport -------------------------------------------------


async def test_raccoon_success(httpx_mock, client):
    httpx_mock.add_response(url=URL, method="POST", json={"known": ["A:B"], "unknown": []})
    result = await check_lint(_doc(_node("l", "pv-label", props={"pv": "ca://A:B"})), _catalog(), client=client)
    assert result.findings == [] and result.skipped == []
    sent = json.loads(httpx_mock.get_requests()[0].content)
    assert sent == {"pvs": ["A:B"]}


async def test_raccoon_unknown_name_warns_on_node(httpx_mock, client):
    httpx_mock.add_response(url=URL, method="POST", json={"known": ["A:B"], "unknown": ["NO:PE"]})
    doc = _doc(
        _node("a", "pv-label", props={"pv": "A:B"}),
        _node("b", "pv-label", props={"pv": "NO:PE"}),
        _node("c", "pv-label", props={"pv": "pva://NO:PE"}),
    )
    result = await check_lint(doc, _catalog(), client=client)
    assert _codes(result.findings) == [("b", "unknown_pv", "warning"), ("c", "unknown_pv", "warning")]
    assert result.findings[0].path == "/root/children/1/props/pv"
    # de-duplicated before the POST
    assert json.loads(httpx_mock.get_requests()[0].content) == {"pvs": ["A:B", "NO:PE"]}


async def test_raccoon_500_adapter_misconfigured_is_skipped(httpx_mock, client):
    httpx_mock.add_response(
        url=URL, method="POST", status_code=500, json={"code": "adapter_misconfigured", "detail": "no adapter"}
    )
    doc = _doc(_node("b", "pv-label", props={"pv": "NO:PE"}))
    result = await check_lint(doc, _catalog(), client=client)
    assert result.findings == []
    assert result.skipped == [{"check": "raccoon", "reason": "adapter_misconfigured"}]


async def test_raccoon_400_is_skipped(httpx_mock, client):
    httpx_mock.add_response(url=URL, method="POST", status_code=400, text="bad")
    result = await check_lint(_doc(_node("b", "pv-label", props={"pv": "NO:PE"})), _catalog(), client=client)
    assert result.findings == []
    assert result.skipped == [{"check": "raccoon", "reason": "400"}]


async def test_raccoon_timeout_is_skipped(httpx_mock, client):
    httpx_mock.add_exception(httpx.ReadTimeout("slow"), url=URL, method="POST")
    result = await check_lint(_doc(_node("b", "pv-label", props={"pv": "NO:PE"})), _catalog(), client=client)
    assert result.findings == []
    assert result.skipped == [{"check": "raccoon", "reason": "timeout"}]
    assert httpx_mock.get_requests()[0].extensions["timeout"]["read"] == RACCOON_TIMEOUT_S


async def test_raccoon_connection_error_is_skipped(httpx_mock, client):
    httpx_mock.add_exception(httpx.ConnectError("refused"), url=URL, method="POST")
    result = await check_lint(_doc(_node("b", "pv-label", props={"pv": "NO:PE"})), _catalog(), client=client)
    assert result.skipped == [{"check": "raccoon", "reason": "ConnectError"}]


async def test_pv_not_checkable_kept_out_of_post(httpx_mock, client):
    httpx_mock.add_response(url=URL, method="POST", json={"known": ["A:B"], "unknown": []})
    doc = _doc(
        _node("a", "pv-label", props={"pv": "A:B"}),
        _node("p", "pv-label", props={"pv": "SR*:X"}),
        _node("m", "pv-label", props={"pv": "$(P):X"}),
    )
    result = await check_lint(doc, _catalog(), client=client)
    assert _codes(result.findings) == [("p", "pv_not_checkable", "warning"), ("m", "pv_not_checkable", "warning")]
    assert json.loads(httpx_mock.get_requests()[0].content) == {"pvs": ["A:B"]}


async def test_no_checkable_names_makes_no_request(httpx_mock, client):
    doc = _doc(_node("a", "pv-label", props={"pv": "loc://x"}), _node("b", "pv-label", props={"pv": "${P}"}))
    result = await check_lint(doc, _catalog(), client=client)
    assert result.findings == [] and result.skipped == []
    assert httpx_mock.get_requests() == []


async def test_raccoon_batches_at_limit(httpx_mock, client):
    from canopy_mcp.check.lint import PvRef

    httpx_mock.add_response(url=URL, method="POST", json={"known": [], "unknown": []}, is_reusable=True)
    refs = [PvRef(f"PV:{i}", "n", ("root",)) for i in range(RACCOON_BATCH + 1)]
    findings, skipped = await pv_findings(refs, client=client)
    requests = httpx_mock.get_requests()
    assert [len(json.loads(r.content)["pvs"]) for r in requests] == [RACCOON_BATCH, 1]
    assert findings == [] and skipped == []


# --- roles ------------------------------------------------------------------


@pytest.fixture
def default_scheme(monkeypatch):
    monkeypatch.delenv(THEME_FILE_ENV, raising=False)
    clear_color_scheme_cache()
    yield
    clear_color_scheme_cache()


def _role(name, fallback="#102030"):
    return {"$role": name, "fallback": fallback}


async def test_known_role_no_finding(default_scheme):
    node = _node("f", "frame", props={"backgroundColor": _role("Display_Background", "#ffffff")})
    result = await check_lint(_doc(node), _catalog())
    assert result.findings == []


async def test_unknown_role_in_prop_warns(default_scheme):
    node = _node("f", "frame", props={"backgroundColor": _role("site.beamline_blue")})
    result = await check_lint(_doc(node), _catalog())
    assert _codes(result.findings) == [("f", "unknown_role", "warning")]
    finding = result.findings[0]
    assert finding.path == "/root/children/0/props/backgroundColor"
    assert "site.beamline_blue" in finding.message
    assert "'f'" in finding.message
    assert "fallback" in finding.message


async def test_unknown_role_in_nested_array_warns(default_scheme):
    curves = [{"y_channel": "loc://a", "color": _role("Text")}, {"y_channel": "loc://b", "color": _role("Nope")}]
    result = await check_lint(_doc(_node("w", "waveform-plot", props={"curves": curves})), _catalog())
    assert _codes(result.findings) == [("w", "unknown_role", "warning")]
    assert result.findings[0].path == "/root/children/0/props/curves/1/color"


async def test_unknown_role_in_rule_default_warns(default_scheme):
    rule = {
        "name": "alarm",
        "property": "foregroundColor",
        "pvs": [{"name": "loc://x"}],
        "conditions": [{"op": "==", "value": _role("MAJOR")}],
        "default": _role("Mystery"),
    }
    result = await check_lint(_doc(_node("f", "frame", props={}, rules=[rule])), _catalog())
    assert _codes(result.findings) == [("f", "unknown_role", "warning")]
    assert result.findings[0].path == "/root/children/0/rules/0/default"
    assert "Mystery" in result.findings[0].message


async def test_unknown_role_in_rule_condition_value_warns(default_scheme):
    rule = {"conditions": [{"op": "==", "value": 1}, {"op": "==", "value": _role("Odd")}], "default": "#000000"}
    result = await check_lint(_doc(_node("f", "frame", props={}, rules=[rule])), _catalog())
    assert _codes(result.findings) == [("f", "unknown_role", "warning")]
    assert result.findings[0].path == "/root/children/0/rules/0/conditions/1/value"


def test_role_findings_skip_without_scheme():
    doc = _doc(_node("f", "frame", props={"backgroundColor": _role("Anything")}))
    assert role_findings(doc, None) == []
    assert _codes(role_findings(doc, ["Text"])) == [("f", "unknown_role", "warning")]


def test_role_findings_tolerate_malformed_input():
    doc = _doc(
        _node("f", "frame", props=["not", "a", "dict"], rules="nope"),
        _node("g", "frame", rules=[None, {"conditions": 3}]),
    )
    assert role_findings(doc, ["Text"]) == []
    assert role_findings(None, ["Text"]) == []


# --- fixtures ---------------------------------------------------------------


def test_write_capable_types_are_ir_types():
    ir_types = set(json.loads(IR_MAP_FIXTURE.read_text()))
    assert WRITE_CAPABLE_TYPES <= ir_types
