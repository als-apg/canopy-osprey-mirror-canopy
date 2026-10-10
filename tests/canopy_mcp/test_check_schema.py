"""Schema pass: the staged Screen-IR schema, errors mapped to the nearest node id.

The spec-present cases run on the synthetic schemas of ``synthetic_spec``,
staged into ``tmp_path``; without a staged schema the pass is one
``spec_unavailable`` warning.
"""

from __future__ import annotations

import copy
from typing import Any
from pathlib import Path

import pytest

from canopy_mcp.check import Finding, pointer, node_id_at
from canopy_mcp.check import check_schema as check_staged
from canopy_mcp.store import DraftStore
from canopy_mcp.config import Settings
from canopy_mcp.catalog import build_catalog
from canopy_mcp.tools.check import run_check
from canopy_mcp.check.schema import (
    SUPPORTED_VERSIONS,
    SpecUnavailable,
    spec_note,
    load_schema,
)
from tests.canopy_mcp.synthetic_spec import write_spec


@pytest.fixture
def spec(tmp_path: Path) -> Path:
    return write_spec(tmp_path / "spec")


@pytest.fixture
def check_schema(spec: Path):
    """``check_schema`` against the staged synthetic spec."""
    return lambda doc: check_staged(doc, spec)


def _node(nid: str, children: list[dict[str, Any]] | None = None, **extra: Any) -> dict[str, Any]:
    node: dict[str, Any] = {
        "id": nid,
        "type": "label",
        "props": {"text": nid},
        "geometry": {"x": 0, "y": 0, "width": 10, "height": 10},
    }
    if children is not None:
        node["children"] = children
    node.update(extra)
    return node


def _doc() -> dict[str, Any]:
    return {
        "schemaVersion": "1.0",
        "kind": "screen",
        "id": "demo",
        "metadata": {
            "title": "Demo",
            "source": {"type": "native"},
            "size": {"width": 100, "height": 100},
        },
        "macros": [],
        "root": _node("w-root", [_node("w-a", [_node("w-b")]), _node("w-c")]),
    }


def _role_rule() -> dict[str, Any]:
    """A conditional rule whose default and condition value are theme colour roles (1.1 only)."""
    return {
        "id": "r-1",
        "name": "alarm colour",
        "targetProperty": "foreground",
        "default": {"$role": "Text", "fallback": "#202020", "fallbackDark": "#e0e0e0"},
        "pvs": [],
        "conditions": [
            {"expression": "True", "value": {"$role": "MAJOR", "fallback": "#ff0000"}},
            {"expression": "False", "value": "#00ff00"},
        ],
    }


def _role_doc() -> dict[str, Any]:
    doc = _doc()
    doc["schemaVersion"] = "1.1"
    doc["root"]["children"][1]["rules"] = [_role_rule()]
    return doc


def test_schema_loads_staged_2020_12_schema(spec: Path) -> None:
    schema = load_schema(spec_dir=spec)
    assert schema["$schema"].endswith("2020-12/schema")
    assert "WidgetNode" in schema["$defs"]


def test_load_schema_defaults_to_newest_supported_version(spec: Path) -> None:
    schema = load_schema(spec_dir=spec)
    assert schema["$id"].endswith("/screen-ir/1.1/schema.json")
    assert "ColorRole" in schema["$defs"]
    assert load_schema("1.1", spec) is schema
    assert load_schema("1.0", spec)["properties"]["schemaVersion"] == {"const": "1.0"}


def test_supported_versions_are_a_constant_without_any_spec() -> None:
    assert SUPPORTED_VERSIONS == ("1.0", "1.1")


def test_a_changed_schema_file_is_read_again(spec: Path) -> None:
    path = spec / "ir" / "screen-ir-1.1.schema.json"
    before = load_schema("1.1", spec)
    path.write_text('{"$id": "changed", "type": "object"}')
    import os

    os.utime(path, ns=(path.stat().st_atime_ns, path.stat().st_mtime_ns + 10_000_000))
    assert load_schema("1.1", spec) == {"$id": "changed", "type": "object"}
    assert load_schema("1.1", spec) is not before


# --- spec unavailable -----------------------------------------------------------


def test_without_a_spec_dir_the_pass_is_one_spec_unavailable_warning() -> None:
    (f,) = check_staged(_doc(), None)
    assert (f.code, f.severity, f.path, f.node_id) == ("spec_unavailable", "warning", "", None)
    assert "CANOPY_SPEC_DIR" in f.message


def test_a_missing_spec_dir_is_named(tmp_path: Path) -> None:
    (f,) = check_staged(_doc(), tmp_path / "absent")
    assert f.code == "spec_unavailable"
    assert str(tmp_path / "absent") in f.message


def test_a_missing_version_schema_is_named(tmp_path: Path) -> None:
    spec = write_spec(tmp_path / "spec", versions=("1.1",))
    (f,) = check_staged(_doc(), spec)  # declares 1.0
    assert f.code == "spec_unavailable" and f.severity == "warning"
    assert "schemaVersion 1.0" in f.message and "screen-ir-1.0.schema.json" in f.message
    doc = _doc()
    doc["schemaVersion"] = "1.1"
    assert check_staged(doc, spec) == []


def test_an_unreadable_schema_is_spec_unavailable(spec: Path) -> None:
    (spec / "ir" / "screen-ir-1.0.schema.json").write_text("not json")
    (f,) = check_staged(_doc(), spec)
    assert f.code == "spec_unavailable"
    assert "cannot read" in f.message
    with pytest.raises(SpecUnavailable):
        load_schema("1.0", spec)


def test_spec_note_is_structured(tmp_path: Path, spec: Path) -> None:
    assert spec_note(spec) is None
    assert spec_note(spec, "1.1") is None
    note = spec_note(None)
    assert note is not None and note["code"] == "spec_unavailable" and note["message"]
    assert spec_note(tmp_path / "absent")["code"] == "spec_unavailable"
    (spec / "ir" / "screen-ir-1.1.schema.json").unlink()
    assert spec_note(spec) is None  # the directory is there
    assert spec_note(spec, "1.1")["code"] == "spec_unavailable"


def test_the_spec_dir_defaults_to_spec_beside_the_viewer_dist(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("CANOPY_VIEWER_DIST", raising=False)
    assert Settings(viewer_dist=None).staged_spec_dir is None
    assert Settings(viewer_dist=tmp_path / "dist").staged_spec_dir == tmp_path / "spec"
    assert Settings(viewer_dist=tmp_path / "dist", spec_dir=tmp_path / "s").staged_spec_dir == tmp_path / "s"
    monkeypatch.setenv("CANOPY_SPEC_DIR", str(tmp_path / "env"))
    assert Settings().staged_spec_dir == tmp_path / "env"
    monkeypatch.setenv("CANOPY_SPEC_DIR", "")
    assert Settings(viewer_dist=tmp_path / "dist").staged_spec_dir == tmp_path / "spec"


async def test_check_without_a_spec_reports_it_once_and_runs_the_other_passes(tmp_path: Path) -> None:
    """No spec: one spec_unavailable, no prop lint; semantic findings (writes_to_machine) unchanged."""
    catalog = build_catalog(
        {"label": {"consumes": ["text"], "verbatim": True}, "pv-text-input": {"consumes": ["pv"], "verbatim": False}},
        {"label": {"type": "object", "properties": {"text": {"type": "number"}}}},
    )
    doc = _doc()
    doc["root"]["children"].append(_node("w-x", type="nope"))
    doc["root"]["children"].append(_node("w-in", type="pv-text-input", props={"pv": "SR:X"}))
    settings = Settings(viewer_dist=None, drafts_dir=tmp_path)
    result = await run_check(doc, store=DraftStore(tmp_path), catalog=catalog, settings=settings)
    assert [(f.code, f.severity) for f in result.schema] == [("spec_unavailable", "warning")]
    # Prop lint does not run (text is not a number, label is verbatim); unknown types still do.
    assert [f.code for f in result.lint] == ["unknown_type"]
    assert result.errors == [f for f in result.lint if f.code == "unknown_type"]
    assert "inert_in_osprey" in [f.code for f in result.semantic]
    with_spec = await run_check(
        doc,
        store=DraftStore(tmp_path),
        catalog=catalog,
        settings=Settings(viewer_dist=None, drafts_dir=tmp_path, spec_dir=write_spec(tmp_path / "spec")),
    )
    assert with_spec.schema == []
    # Every label's text fails its prop schema once the spec is staged; semantic is the same either way.
    assert sorted(f.code for f in with_spec.lint) == ["invalid_prop"] * 4 + ["unknown_type"]
    assert with_spec.semantic == result.semantic


def test_role_bearing_1_1_document_passes_schema(check_schema) -> None:
    assert check_schema(_role_doc()) == []


def test_1_1_document_without_roles_passes_schema(check_schema) -> None:
    doc = _doc()
    doc["schemaVersion"] = "1.1"
    assert check_schema(doc) == []


def test_role_in_1_0_document_is_rejected(check_schema) -> None:
    """A 1.0 document is checked against the frozen 1.0 schema, which has no colour roles."""
    doc = _role_doc()
    doc["schemaVersion"] = "1.0"
    findings = check_schema(doc)
    assert findings
    assert {f.node_id for f in findings} == {"w-c"}
    assert all(f.path.startswith("/root/children/1/rules/0") for f in findings)


def test_malformed_role_is_rejected_in_1_1(check_schema) -> None:
    doc = _role_doc()
    doc["root"]["children"][1]["rules"][0]["default"] = {"$role": "text.primary", "fallback": "red"}
    findings = check_schema(doc)
    assert findings
    assert {f.node_id for f in findings} == {"w-c"}
    assert all(f.path.startswith("/root/children/1/rules/0") for f in findings)


@pytest.mark.parametrize("version", ["2.0", "1.2", 1.1, None])
def test_unsupported_version_is_rejected(check_schema, version: Any) -> None:
    doc = _doc()
    doc["schemaVersion"] = version
    (f,) = check_schema(doc)
    assert f.path == "/schemaVersion" and f.node_id is None and f.severity == "error"


def test_missing_version_is_rejected(check_schema) -> None:
    doc = _doc()
    del doc["schemaVersion"]
    (f,) = check_schema(doc)
    assert f.path == "" and "schemaVersion" in f.message


async def test_role_bearing_1_1_draft_passes_check(tmp_path: Path, spec: Path) -> None:
    """FR7: schema, semantic and lint passes all accept a role-bearing 1.1 draft."""
    catalog = build_catalog({"label": {"consumes": ["text"], "verbatim": False}})
    settings = Settings(viewer_dist=None, drafts_dir=tmp_path, spec_dir=spec)
    result = await run_check(_role_doc(), store=DraftStore(tmp_path), catalog=catalog, settings=settings)
    assert result.errors == []
    assert result.to_dict() == {"schema": [], "semantic": [], "lint": [], "skipped": []}


def test_role_bearing_1_1_draft_passes_pydm_validation() -> None:
    """FR7: the PyDM converter's own vendored schema accepts the same draft."""
    from pydmconverter.ir.schema import validate_screen_json

    assert validate_screen_json(_role_doc()) == []


def test_valid_document_has_no_findings(check_schema) -> None:
    assert check_schema(_doc()) == []


def test_error_in_nested_node_maps_to_that_node(check_schema) -> None:
    doc = _doc()
    del doc["root"]["children"][0]["children"][0]["geometry"]
    findings = check_schema(doc)
    assert len(findings) == 1
    f = findings[0]
    assert f.code == "schema" and f.severity == "error"
    assert f.node_id == "w-b"
    assert f.path == "/root/children/0/children/0"
    assert "geometry" in f.message


def test_error_inside_node_field_maps_to_enclosing_node(check_schema) -> None:
    doc = _doc()
    doc["root"]["children"][1]["geometry"]["width"] = "wide"
    (f,) = check_schema(doc)
    assert f.node_id == "w-c"
    assert f.path == "/root/children/1/geometry/width"


def test_unknown_node_key_is_reported_on_the_node(check_schema) -> None:
    doc = _doc()
    doc["root"]["children"][0]["bogus"] = 1
    (f,) = check_schema(doc)
    assert f.node_id == "w-a"
    assert f.path == "/root/children/0"


def test_error_outside_tree_has_no_node(check_schema) -> None:
    doc = _doc()
    doc["metadata"]["size"]["width"] = "x"
    doc["schemaVersion"] = "2.0"
    findings = check_schema(doc)
    assert {f.path for f in findings} == {"/metadata/size/width", "/schemaVersion"}
    assert all(f.node_id is None for f in findings)


def test_missing_top_level_key_path_is_document_root(check_schema) -> None:
    doc = _doc()
    del doc["root"]
    (f,) = check_schema(doc)
    assert f.path == "" and f.node_id is None


def test_findings_are_deterministic(check_schema) -> None:
    doc = _doc()
    doc["root"]["children"][1]["geometry"]["width"] = "wide"
    doc["metadata"]["size"]["height"] = "tall"
    assert check_schema(copy.deepcopy(doc)) == check_schema(doc)
    paths = [f.path for f in check_schema(doc)]
    assert paths == sorted(paths)


def test_node_id_at_falls_back_to_nearest_ancestor_with_id() -> None:
    doc = _doc()
    del doc["root"]["children"][0]["id"]
    assert node_id_at(doc, ["root", "children", 0, "props"]) == "w-root"
    assert node_id_at(doc, ["root", "children", 0, "children", 0, "type"]) == "w-b"
    assert node_id_at(doc, ["root", "children", 9]) == "w-root"
    assert node_id_at(doc, []) is None
    assert node_id_at(doc, ["formulas", 0]) is None


def test_pointer_escapes_rfc6901() -> None:
    assert pointer([]) == ""
    assert pointer(["root", "props", "a/b~c", 0]) == "/root/props/a~1b~0c/0"


def test_finding_to_dict_is_camel_case() -> None:
    f = Finding("w-1", "schema", "/root", "error", "bad")
    assert f.to_dict() == {
        "nodeId": "w-1",
        "code": "schema",
        "path": "/root",
        "severity": "error",
        "message": "bad",
    }
    assert f.key == ("w-1", "schema", "/root")
    g = Finding("w-1", "unknown_widget", "/root/type", "warning", "m", doc_id="a/b", original_class="X")
    assert g.to_dict()["docId"] == "a/b" and g.to_dict()["originalClass"] == "X"
