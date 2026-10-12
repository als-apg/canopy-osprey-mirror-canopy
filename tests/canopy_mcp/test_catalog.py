"""The widget catalog: the viewer's ir-map.json, with prop schemas from the staged spec."""

import json
import shutil
from pathlib import Path

import pytest
from fastmcp.exceptions import ToolError

from canopy_mcp import catalog as catalog_mod
from canopy_mcp.tools import widgets as widget_tools
from canopy_mcp.config import Settings
from canopy_mcp.catalog import (
    SOURCE_CATALOG,
    PLACEHOLDER_TYPE,
    CatalogError,
    load_ir_map,
    load_catalog,
    build_catalog,
    ensure_catalog,
    load_prop_schemas,
)
from tests.canopy_mcp.synthetic_spec import COLOR_DEF, PLOT_PROPS, write_spec

FIXTURES = Path(__file__).parent / "fixtures"
IR_MAP_FIXTURE = FIXTURES / "ir-map.json"

#: The verbatim types of the fixture ir-map: undocumented until a spec gives them a prop schema.
VERBATIM = {
    "arc", "ellipse", "group", "line", "polygon", "rectangle", "pv-gauge", "pv-meter", "pv-radio-group",
    "pv-spinbox", "pv-tabs", "pv-tank", "pv-thermometer", "pv-toggle",
}  # fmt: skip

PV_LED_SCHEMA = {
    "type": "object",
    "required": ["pv"],
    "properties": {"pv": {"type": "string"}, "onValue": {"type": "number"}},
}


@pytest.fixture
def viewer_dist(tmp_path: Path) -> Path:
    dist = tmp_path / "dist"
    dist.mkdir()
    shutil.copy(IR_MAP_FIXTURE, dist / "ir-map.json")
    return dist


@pytest.fixture
def settings(viewer_dist: Path) -> Settings:
    return Settings(base_url="http://canopy.test", api_key="read-key", viewer_dist=viewer_dist)


@pytest.fixture(autouse=True)
def reset_catalog():
    catalog_mod.set_catalog(None)
    yield
    catalog_mod.set_catalog(None)


def _ir_map() -> dict:
    return json.loads(IR_MAP_FIXTURE.read_text())


# --- ir-map.json ------------------------------------------------------------


def test_load_ir_map_reads_viewer_dist(viewer_dist):
    ir_map = load_ir_map(viewer_dist)
    assert ir_map["rectangle"]["consumes"] == [] and ir_map["rectangle"]["verbatim"] is True
    assert ir_map["embedded-display"]["consumes"] == ["file", "macros"]


def test_every_fixture_entry_carries_the_editor_metadata():
    for widget_type, entry in _ir_map().items():
        assert isinstance(entry["displayName"], str) and entry["displayName"], widget_type
        assert isinstance(entry["description"], str), widget_type
        assert isinstance(entry["category"], str), widget_type
        assert isinstance(entry["supportsChildren"], bool), widget_type


def test_load_ir_map_requires_viewer_dist():
    with pytest.raises(CatalogError, match="CANOPY_VIEWER_DIST"):
        load_ir_map(None)


def test_load_ir_map_missing_file(tmp_path):
    with pytest.raises(CatalogError, match="ir-map.json"):
        load_ir_map(tmp_path)


@pytest.mark.parametrize("content", ["not json", "{}", '{"x": 3}'])
def test_load_ir_map_rejects_malformed(tmp_path, content):
    (tmp_path / "ir-map.json").write_text(content)
    with pytest.raises(CatalogError):
        load_ir_map(tmp_path)


# --- staged prop schemas ------------------------------------------------------


def test_prop_schemas_are_read_by_ir_id(tmp_path):
    spec = write_spec(tmp_path / "spec", props={"pv-led": PV_LED_SCHEMA, "arc": {"type": "object"}})
    (spec / "widgets" / "README.md").write_text("not a schema")
    assert load_prop_schemas(spec) == {**PLOT_PROPS, "arc": {"type": "object"}, "pv-led": PV_LED_SCHEMA}


def test_no_spec_means_no_prop_schemas(tmp_path):
    assert load_prop_schemas(None) == {}
    assert load_prop_schemas(tmp_path / "absent") == {}
    (tmp_path / "bare").mkdir()
    assert load_prop_schemas(tmp_path / "bare") == {}


@pytest.mark.parametrize("content", ["not json", "[1, 2]"])
def test_a_broken_prop_schema_is_a_catalog_error(tmp_path, content):
    spec = write_spec(tmp_path / "spec")
    (spec / "widgets" / "arc.props.schema.json").write_text(content)
    with pytest.raises(CatalogError, match="arc.props.schema.json"):
        load_prop_schemas(spec)


# --- Build ------------------------------------------------------------------


def test_every_ir_type_has_an_entry_with_its_metadata():
    ir_map = _ir_map()
    catalog = build_catalog(ir_map)
    # unknown-widget is the ONE type outside ir-map.json.
    assert set(catalog.entries) - set(ir_map) == {PLACEHOLDER_TYPE}
    for widget_type, mapping in ir_map.items():
        entry = catalog.get(widget_type)
        assert entry.display_name == mapping["displayName"]
        assert entry.description == mapping["description"]
        assert entry.category == mapping["category"]
        assert entry.supports_children == mapping["supportsChildren"]
    # Ids that share a runtime type no longer share a label.
    assert catalog.get("pv-button").display_name != catalog.get("regular-button").display_name


def test_verbatim_types_without_a_prop_schema_are_undocumented():
    catalog = build_catalog(_ir_map())
    undocumented = {t for t, e in catalog.entries.items() if e.undocumented}
    assert undocumented == VERBATIM
    assert catalog.get("pv-toggle").props == {}
    assert catalog.get("pv-toggle").prop_schema is None


def test_a_staged_prop_schema_documents_a_verbatim_type():
    catalog = build_catalog(_ir_map(), {"rectangle": {"type": "object", "properties": {"lineWidth": {}}}})
    assert catalog.get("rectangle").verbatim and not catalog.get("rectangle").undocumented
    assert catalog.get("rectangle").props["lineWidth"].sources == (SOURCE_CATALOG,)


def test_props_are_the_union_tagged_by_source():
    ir_map = {"pv-led": {"consumes": ["pv", "bit"], "verbatim": False, "displayName": "PV LED"}}
    entry = build_catalog(ir_map, {"pv-led": PV_LED_SCHEMA}).get("pv-led")

    assert entry.props["pv"].sources == ("catalog", "consumes")
    assert entry.props["pv"].required is True
    assert entry.props["pv"].schema == {"type": "string"}
    assert entry.props["onValue"].sources == ("catalog",)
    assert entry.props["bit"].sources == ("consumes",)
    assert entry.props["bit"].schema is None
    assert entry.prop_schema == PV_LED_SCHEMA
    assert not entry.undocumented
    assert entry.display_name == "PV LED"


def test_a_prop_schema_for_an_id_outside_ir_map_adds_no_entry():
    catalog = build_catalog({"x": {"consumes": []}}, {"only-in-spec": {}})
    assert "only-in-spec" not in catalog


def test_placeholder_entry():
    entry = build_catalog(_ir_map()).get(PLACEHOLDER_TYPE)
    assert entry.placeholder
    assert not entry.undocumented
    assert entry.prop_schema is None


# --- Load -------------------------------------------------------------------


async def test_load_catalog_end_to_end(settings):
    catalog = await load_catalog(settings)
    assert len(catalog.entries) == 38 + 1
    assert all(e.prop_schema is None for e in catalog.entries.values())


async def test_load_catalog_reads_the_spec_beside_the_viewer_dist(settings, viewer_dist):
    write_spec(viewer_dist.parent / "spec", props={"pv-led": PV_LED_SCHEMA})
    catalog = await load_catalog(settings)
    assert catalog.get("pv-led").prop_schema == PV_LED_SCHEMA


async def test_load_catalog_prefers_canopy_spec_dir(settings, viewer_dist, tmp_path):
    write_spec(viewer_dist.parent / "spec", props={"pv-led": {"type": "object"}})
    other = write_spec(tmp_path / "other", props={"pv-led": PV_LED_SCHEMA})
    catalog = await load_catalog(settings.model_copy(update={"spec_dir": other}))
    assert catalog.get("pv-led").prop_schema == PV_LED_SCHEMA


async def test_load_catalog_without_viewer_dist():
    with pytest.raises(CatalogError, match="CANOPY_VIEWER_DIST"):
        await load_catalog(Settings(base_url="http://canopy.test", viewer_dist=None))


async def test_ensure_catalog_loads_once(settings, monkeypatch):
    loads = []
    real = catalog_mod.load_ir_map

    def counting(viewer_dist):
        loads.append(viewer_dist)
        return real(viewer_dist)

    monkeypatch.setattr(catalog_mod, "load_ir_map", counting)
    first = await ensure_catalog(settings)
    second = await ensure_catalog(settings)
    assert first is second
    assert len(loads) == 1


# --- Tools ------------------------------------------------------------------


@pytest.fixture
def loaded():
    catalog = build_catalog(_ir_map(), {"pv-led": PV_LED_SCHEMA})
    catalog_mod.set_catalog(catalog)
    return catalog


@pytest.fixture
def staged(monkeypatch, tmp_path) -> Path:
    """A synthetic spec named by CANOPY_SPEC_DIR, as the tools read it."""
    spec = write_spec(tmp_path / "staged")
    monkeypatch.setenv("CANOPY_SPEC_DIR", str(spec))
    return spec


@pytest.fixture
def unstaged(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("CANOPY_VIEWER_DIST", str(tmp_path / "dist"))  # no spec/ beside it


async def test_list_widgets_tool(loaded, staged):
    result = await widget_tools.list_widgets()
    assert result["total"] == 39
    by_type = {w["type"]: w for w in result["widgets"]}
    assert set(by_type) == set(loaded.entries)
    assert by_type["pv-tank"]["undocumented"] is True
    assert by_type[PLACEHOLDER_TYPE]["placeholder"] is True
    assert by_type["pv-led"]["displayName"] == loaded.get("pv-led").display_name
    assert "note" not in result
    json.dumps(result)


async def test_list_widgets_without_a_spec_carries_a_note(loaded, unstaged):
    result = await widget_tools.list_widgets()
    assert result["total"] == 39
    assert all(w["displayName"] for w in result["widgets"])
    assert result["note"]["code"] == "spec_unavailable"
    assert result["note"]["message"]
    json.dumps(result)


async def test_get_widget_tool(loaded, staged):
    result = await widget_tools.get_widget("pv-led")
    assert result["type"] == "pv-led"
    assert result["props"]["pv"]["sources"] == ["catalog", "consumes"]
    assert result["props"]["bit"]["sources"] == ["consumes"]
    assert result["propSchema"]["properties"]["pv"]["type"] == "string"
    assert "note" not in result
    json.dumps(result)


async def test_get_widget_without_a_spec_carries_a_note(loaded, unstaged):
    result = await widget_tools.get_widget("pv-tank")
    assert result["undocumented"] is True and result["propSchema"] is None
    assert result["note"]["code"] == "spec_unavailable"


async def test_get_widget_tool_unknown_type(loaded, unstaged):
    with pytest.raises(ToolError, match="no-such-widget"):
        await widget_tools.get_widget("no-such-widget")


async def test_get_schema_tool(staged):
    schema = await widget_tools.get_schema()
    assert schema["$schema"].startswith("https://json-schema.org/draft/2020-12")
    assert schema["$id"].endswith("/screen-ir/1.1/schema.json")
    assert schema["properties"]["schemaVersion"]["enum"] == ["1.0", "1.1"]
    assert "ColorRole" in schema["$defs"]
    # Each call returns a fresh copy; mutating one does not leak.
    schema["properties"] = {}
    assert (await widget_tools.get_schema())["properties"]


async def test_get_schema_without_a_spec_is_a_note_not_an_error(unstaged):
    result = await widget_tools.get_schema()
    assert result["schema"] is None
    assert result["note"]["code"] == "spec_unavailable"
    assert "spec" in result["note"]["message"]
    json.dumps(result)


async def test_get_schema_with_a_spec_missing_the_newest_version(monkeypatch, tmp_path):
    spec = write_spec(tmp_path / "staged", versions=("1.0",))
    monkeypatch.setenv("CANOPY_SPEC_DIR", str(spec))
    result = await widget_tools.get_schema()
    assert result["schema"] is None
    assert "1.1" in result["note"]["message"]


async def test_tools_registered_on_mcp():
    from canopy_mcp.server import mcp

    names = {tool.name for tool in await mcp.list_tools()}
    assert {"list_widgets", "get_widget", "get_schema"} <= names


# --- Colour roles (the spec's inline colour def) --------------------------------

_ROLE = {"$role": "text.primary", "fallback": "#112233", "fallbackDark": "#eeeeee"}


#: Synthetic prop schemas carrying the colour def on their colour props.
ROLE_PROPS = {
    "pv-label": {
        "type": "object",
        "properties": {"pv": {"type": "string"}, "foregroundColor": COLOR_DEF, "backgroundColor": COLOR_DEF},
    },
    "xy-plot": {
        "type": "object",
        "properties": {
            "curves": {
                "type": "array",
                "items": {"type": "object", "properties": {"yPv": {"type": "string"}, "color": COLOR_DEF}},
            }
        },
    },
}


def _role_catalog():
    return build_catalog(_ir_map(), ROLE_PROPS)


def _role_doc(**props):
    return {
        "schemaVersion": "1.1",
        "kind": "screen",
        "id": "t",
        "root": {
            "id": "root",
            "type": "frame",
            "geometry": {"x": 0, "y": 0, "width": 100, "height": 100},
            "children": [
                {
                    "id": "n1",
                    "type": "pv-label",
                    "geometry": {"x": 0, "y": 0, "width": 10, "height": 10},
                    "props": {"pv": "SR:X", **props},
                }
            ],
        },
    }


def test_catalog_exposes_the_colour_def_on_colour_props():
    entry = _role_catalog().get("pv-label")
    schema = entry.props["foregroundColor"].schema
    assert schema["anyOf"][0] == {"type": "string"}
    assert schema["anyOf"][1]["required"] == ["$role", "fallback"]


def test_a_role_bearing_draft_passes_prop_validation():
    from canopy_mcp.check.lint import prop_findings

    doc = _role_doc(foregroundColor=_ROLE, backgroundColor={"$role": "surface", "fallback": "#ffffff"})
    assert prop_findings(doc, _role_catalog()) == []


def test_a_role_in_a_nested_plot_curve_passes_prop_validation():
    from canopy_mcp.check.lint import prop_findings

    doc = _role_doc()
    doc["root"]["children"][0].update(
        type="xy-plot", props={"curves": [{"yPv": "SR:Y", "color": {"$role": "series.1", "fallback": "#336699"}}]}
    )
    assert [f for f in prop_findings(doc, _role_catalog()) if "color" in f.path] == []


@pytest.mark.parametrize(
    "bad",
    [
        {"$role": "text.primary"},
        {"$role": "text.primary", "fallback": "red"},
        {"$role": "", "fallback": "#112233"},
        {"$role": "text.primary", "fallback": "#112233", "extra": 1},
    ],
)
def test_a_malformed_role_is_an_invalid_prop(bad):
    from canopy_mcp.check.lint import prop_findings

    findings = prop_findings(_role_doc(foregroundColor=bad), _role_catalog())
    assert [(f.code, f.path) for f in findings] == [("invalid_prop", "/root/children/0/props/foregroundColor")]
