"""The widget catalog: Beaver registry joined with the viewer's ir-map.json."""

import json
import shutil
from pathlib import Path

import httpx
import pytest
from fastmcp.exceptions import ToolError

from canopy_mcp import catalog as catalog_mod
from canopy_mcp.tools import widgets as widget_tools
from canopy_mcp.client import API_KEY_HEADER, make_client
from canopy_mcp.config import Settings
from canopy_mcp.catalog import (
    WIDGETS_PATH,
    PLACEHOLDER_TYPE,
    CatalogError,
    load_ir_map,
    load_schema,
    load_catalog,
    build_catalog,
    join_coverage,
    ensure_catalog,
    fetch_registry,
)

FIXTURES = Path(__file__).parent / "fixtures"
IR_MAP_FIXTURE = FIXTURES / "ir-map.json"
REGISTRY_DIR = Path(__file__).resolve().parents[2] / "widget-registry-data" / "widgets"

UNDOCUMENTED = {"pv-toggle", "pv-tabs", "pv-gauge", "pv-tank", "pv-thermometer"}


def _registry_details() -> list[dict]:
    """The 32 checked-in Beaver widget files, as GET /beaver/v1/widgets/{id} returns them."""
    return [json.loads(p.read_text()) for p in sorted(REGISTRY_DIR.glob("*.json"))]


def _summary(detail: dict) -> dict:
    return {
        "id": detail["id"],
        "displayName": detail.get("displayName", detail["id"]),
        "category": detail.get("category", "misc"),
        "paletteIcon": detail.get("paletteIcon", ""),
        "version": detail.get("version", "1.0.0"),
        "supportsPv": detail.get("supportsPv", False),
    }


class Registry:
    """A mocked Beaver: the summary list plus per-id details, recording every request."""

    def __init__(self, details: list[dict], *, total: int | None = None, fail: dict[str, int] | None = None):
        self.details = {d["id"]: d for d in details}
        self.total = len(details) if total is None else total
        self.fail = fail or {}
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path in self.fail:
            return httpx.Response(self.fail[path], json={"detail": "boom"})
        if path == WIDGETS_PATH:
            widgets = [_summary(d) for d in self.details.values()] if self.total else []
            return httpx.Response(200, json={"widgets": widgets, "total": self.total})
        widget_id = path.removeprefix(WIDGETS_PATH + "/")
        if widget_id in self.details:
            return httpx.Response(200, json=self.details[widget_id])
        return httpx.Response(404, json={"detail": "not found"})

    def client(self, settings: Settings) -> httpx.AsyncClient:
        return make_client(settings, transport=httpx.MockTransport(self.handler))


@pytest.fixture
def viewer_dist(tmp_path: Path) -> Path:
    shutil.copy(IR_MAP_FIXTURE, tmp_path / "ir-map.json")
    return tmp_path


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


# --- Registry fetch ---------------------------------------------------------


async def test_fetch_lists_then_fetches_every_id_with_read_key(settings):
    details = _registry_details()
    registry = Registry(details)
    async with registry.client(settings) as client:
        fetched = await fetch_registry(client)

    assert {d["id"] for d in fetched} == {d["id"] for d in details}
    assert all("propSchema" in d for d in fetched)
    paths = [r.url.path for r in registry.requests]
    assert paths[0] == WIDGETS_PATH
    assert sorted(paths[1:]) == sorted(f"{WIDGETS_PATH}/{d['id']}" for d in details)
    assert len(paths) == 1 + len(details)
    assert all(r.headers[API_KEY_HEADER] == "read-key" for r in registry.requests)


async def test_fetch_refuses_empty_registry(settings):
    registry = Registry([], total=0)
    async with registry.client(settings) as client:
        with pytest.raises(CatalogError, match="total == 0"):
            await fetch_registry(client)
    assert [r.url.path for r in registry.requests] == [WIDGETS_PATH]


async def test_fetch_fails_on_list_error(settings):
    registry = Registry(_registry_details(), fail={WIDGETS_PATH: 401})
    async with registry.client(settings) as client:
        with pytest.raises(CatalogError, match=WIDGETS_PATH):
            await fetch_registry(client)


async def test_fetch_fails_on_detail_error(settings):
    registry = Registry(_registry_details(), fail={f"{WIDGETS_PATH}/pv-led": 500})
    async with registry.client(settings) as client:
        with pytest.raises(CatalogError, match="pv-led"):
            await fetch_registry(client)


async def test_fetch_fails_on_connection_error(settings):
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    async with make_client(settings, transport=httpx.MockTransport(refuse)) as client:
        with pytest.raises(CatalogError):
            await fetch_registry(client)


# --- ir-map.json ------------------------------------------------------------


def test_load_ir_map_reads_viewer_dist(viewer_dist):
    ir_map = load_ir_map(viewer_dist)
    assert ir_map["rectangle"] == {"consumes": [], "verbatim": True}
    assert ir_map["embedded-display"] == {"consumes": ["file", "macros"], "verbatim": False}


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


# --- Join -------------------------------------------------------------------


def test_join_coverage_every_beaver_id_is_an_ir_type_and_every_ir_type_has_an_entry():
    details = _registry_details()
    ir_map = _ir_map()
    beaver_ids = [d["id"] for d in details]
    assert len(beaver_ids) == 32

    assert join_coverage(beaver_ids, list(ir_map)) == []
    assert set(beaver_ids) <= set(ir_map)

    catalog = build_catalog(details, ir_map)
    assert catalog.unmapped_beaver_ids == ()
    assert set(ir_map) <= set(catalog.entries)
    # unknown-widget is the ONE type outside both sources.
    assert set(catalog.entries) - set(ir_map) - set(beaver_ids) == {PLACEHOLDER_TYPE}


def test_join_coverage_reports_beaver_ids_missing_from_ir_map():
    assert join_coverage(["a", "b", "c"], ["b"]) == ["a", "c"]
    catalog = build_catalog([{"id": "only-in-beaver", "propSchema": {}}], {"x": {"consumes": []}})
    assert catalog.unmapped_beaver_ids == ("only-in-beaver",)
    assert "only-in-beaver" not in catalog


def test_verbatim_types_without_beaver_entry_are_undocumented():
    catalog = build_catalog(_registry_details(), _ir_map())
    undocumented = {t for t, e in catalog.entries.items() if e.undocumented}
    assert undocumented == UNDOCUMENTED
    # A verbatim type Beaver documents is not undocumented.
    assert catalog.get("rectangle").verbatim and not catalog.get("rectangle").undocumented
    assert catalog.get("pv-toggle").props == {}
    assert catalog.get("pv-toggle").prop_schema is None


def test_props_are_the_union_tagged_by_source():
    details = [
        {
            "id": "pv-led",
            "displayName": "PV LED",
            "propSchema": {
                "type": "object",
                "required": ["pv"],
                "properties": {"pv": {"type": "string"}, "onValue": {"type": "number"}},
            },
        }
    ]
    ir_map = {"pv-led": {"consumes": ["pv", "bit"], "verbatim": False}}
    entry = build_catalog(details, ir_map).get("pv-led")

    assert entry.props["pv"].sources == ("beaver", "consumes")
    assert entry.props["pv"].required is True
    assert entry.props["pv"].schema == {"type": "string"}
    assert entry.props["onValue"].sources == ("beaver",)
    assert entry.props["bit"].sources == ("consumes",)
    assert entry.props["bit"].schema is None
    assert entry.prop_schema == details[0]["propSchema"]
    assert not entry.undocumented


def test_real_registry_prop_tags():
    entry = build_catalog(_registry_details(), _ir_map()).get("pv-led")
    # Beaver documents onValue; the renderer consumes bit; both name pv.
    assert entry.props["pv"].sources == ("beaver", "consumes")
    assert entry.props["onValue"].sources == ("beaver",)
    assert entry.props["bit"].sources == ("consumes",)


def test_placeholder_entry():
    entry = build_catalog(_registry_details(), _ir_map()).get(PLACEHOLDER_TYPE)
    assert entry.placeholder
    assert not entry.undocumented
    assert entry.prop_schema is None


# --- Load -------------------------------------------------------------------


async def test_load_catalog_end_to_end(settings):
    registry = Registry(_registry_details())
    async with registry.client(settings) as client:
        catalog = await load_catalog(settings, client)
    assert len(catalog.entries) == 37 + 1


async def test_load_catalog_refuses_empty_registry(settings):
    registry = Registry([], total=0)
    async with registry.client(settings) as client:
        with pytest.raises(CatalogError):
            await load_catalog(settings, client)


async def test_load_catalog_without_viewer_dist_makes_no_request():
    registry = Registry(_registry_details())
    settings = Settings(base_url="http://canopy.test", viewer_dist=None)
    async with registry.client(settings) as client:
        with pytest.raises(CatalogError, match="CANOPY_VIEWER_DIST"):
            await load_catalog(settings, client)
    assert registry.requests == []


async def test_ensure_catalog_loads_once(settings):
    registry = Registry(_registry_details())
    async with registry.client(settings) as client:
        first = await ensure_catalog(settings, client)
        count = len(registry.requests)
        second = await ensure_catalog(settings, client)
    assert first is second
    assert len(registry.requests) == count == 33


# --- Tools ------------------------------------------------------------------


@pytest.fixture
def loaded():
    catalog = build_catalog(_registry_details(), _ir_map())
    catalog_mod.set_catalog(catalog)
    return catalog


async def test_list_widgets_tool(loaded):
    result = await widget_tools.list_widgets()
    assert result["total"] == 38
    by_type = {w["type"]: w for w in result["widgets"]}
    assert set(by_type) == set(loaded.entries)
    assert by_type["pv-tank"]["undocumented"] is True
    assert by_type[PLACEHOLDER_TYPE]["placeholder"] is True
    assert by_type["pv-led"]["displayName"] == "PV LED"
    json.dumps(result)


async def test_get_widget_tool(loaded):
    result = await widget_tools.get_widget("pv-led")
    assert result["type"] == "pv-led"
    assert result["props"]["pv"]["sources"] == ["beaver", "consumes"]
    assert result["props"]["bit"]["sources"] == ["consumes"]
    assert result["propSchema"]["properties"]["pv"]["type"] == "string"
    json.dumps(result)


async def test_get_widget_tool_unknown_type(loaded):
    with pytest.raises(ToolError, match="no-such-widget"):
        await widget_tools.get_widget("no-such-widget")


async def test_get_schema_tool():
    schema = await widget_tools.get_schema()
    assert schema["$schema"].startswith("https://json-schema.org/draft/2020-12")
    assert schema["$id"].endswith("/screen-ir/1.1/schema.json")
    assert schema["properties"]["schemaVersion"]["enum"] == ["1.0", "1.1"]
    assert "ColorRole" in schema["$defs"]
    # Each call returns a fresh copy; mutating one does not leak.
    schema["properties"] = {}
    assert load_schema()["properties"]


async def test_tools_registered_on_mcp():
    from canopy_mcp.server import mcp

    names = {tool.name for tool in await mcp.list_tools()}
    assert {"list_widgets", "get_widget", "get_schema"} <= names
