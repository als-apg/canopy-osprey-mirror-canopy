"""Schema pass: vendored Screen-IR schema, errors mapped to the nearest node id."""

from __future__ import annotations

import copy
from typing import Any

from canopy_mcp.check import Finding, check_schema, load_schema, node_id_at, pointer


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


def test_schema_loads_vendored_2020_12_schema() -> None:
    schema = load_schema()
    assert schema["$schema"].endswith("2020-12/schema")
    assert "WidgetNode" in schema["$defs"]


def test_valid_document_has_no_findings() -> None:
    assert check_schema(_doc()) == []


def test_error_in_nested_node_maps_to_that_node() -> None:
    doc = _doc()
    del doc["root"]["children"][0]["children"][0]["geometry"]
    findings = check_schema(doc)
    assert len(findings) == 1
    f = findings[0]
    assert f.code == "schema" and f.severity == "error"
    assert f.node_id == "w-b"
    assert f.path == "/root/children/0/children/0"
    assert "geometry" in f.message


def test_error_inside_node_field_maps_to_enclosing_node() -> None:
    doc = _doc()
    doc["root"]["children"][1]["geometry"]["width"] = "wide"
    (f,) = check_schema(doc)
    assert f.node_id == "w-c"
    assert f.path == "/root/children/1/geometry/width"


def test_unknown_node_key_is_reported_on_the_node() -> None:
    doc = _doc()
    doc["root"]["children"][0]["bogus"] = 1
    (f,) = check_schema(doc)
    assert f.node_id == "w-a"
    assert f.path == "/root/children/0"


def test_error_outside_tree_has_no_node() -> None:
    doc = _doc()
    doc["metadata"]["size"]["width"] = "x"
    doc["schemaVersion"] = "2.0"
    findings = check_schema(doc)
    assert {f.path for f in findings} == {"/metadata/size/width", "/schemaVersion"}
    assert all(f.node_id is None for f in findings)


def test_missing_top_level_key_path_is_document_root() -> None:
    doc = _doc()
    del doc["root"]
    (f,) = check_schema(doc)
    assert f.path == "" and f.node_id is None


def test_findings_are_deterministic() -> None:
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
