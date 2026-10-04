"""``CANOPY_WIDGETS_DIR``: the catalog built from widget files, never from ``/beaver``."""

import json
import shutil
from pathlib import Path

import httpx
import pytest
from starlette.testclient import TestClient

from canopy_mcp import catalog as catalog_mod
from canopy_mcp.app import create_app, catalog_hook
from canopy_mcp.tools import widgets as widget_tools
from canopy_mcp.client import make_client
from canopy_mcp.config import Settings, get_settings
from canopy_mcp.catalog import (
    PLACEHOLDER_TYPE,
    CatalogError,
    load_catalog,
    build_catalog,
    ensure_catalog,
    load_widget_files,
)

FIXTURES = Path(__file__).parent / "fixtures"
IR_MAP_FIXTURE = FIXTURES / "ir-map.json"
REGISTRY_DIR = Path(__file__).resolve().parents[2] / "widget-registry-data" / "widgets"


class Backend:
    """A backend without Beaver: every request is recorded and answered 404."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(404, json={"detail": "Not Found"})

    def client(self, settings: Settings) -> httpx.AsyncClient:
        return make_client(settings, transport=httpx.MockTransport(self.handler))


@pytest.fixture(autouse=True)
def reset_catalog():
    catalog_mod.set_catalog(None)
    yield
    catalog_mod.set_catalog(None)


@pytest.fixture
def viewer_dist(tmp_path: Path) -> Path:
    dist = tmp_path / "viewer"
    dist.mkdir()
    shutil.copy(IR_MAP_FIXTURE, dist / "ir-map.json")
    return dist


@pytest.fixture
def widgets_dir(tmp_path: Path) -> Path:
    """A copy of the checked-in registry, laid out as the payload ships it."""
    target = tmp_path / "widgets"
    shutil.copytree(REGISTRY_DIR, target)
    return target


@pytest.fixture
def settings(viewer_dist: Path, widgets_dir: Path, tmp_path: Path) -> Settings:
    return Settings(
        base_url="http://canopy.test", viewer_dist=viewer_dist, widgets_dir=widgets_dir, drafts_dir=tmp_path / "drafts"
    )


def _details() -> list[dict]:
    return [json.loads(p.read_text()) for p in sorted(REGISTRY_DIR.glob("*.json"))]


def _ir_map() -> dict:
    return json.loads(IR_MAP_FIXTURE.read_text())


# --- settings ---------------------------------------------------------------


def test_widgets_dir_is_unset_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CANOPY_WIDGETS_DIR", raising=False)
    assert Settings().widgets_dir is None


@pytest.mark.parametrize("value", ["", "/payload/widgets"])
def test_widgets_dir_is_read_from_the_environment(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("CANOPY_WIDGETS_DIR", value)
    assert Settings().widgets_dir == (Path(value) if value else None)


# --- reading the files ------------------------------------------------------


def test_every_widget_file_is_read(widgets_dir: Path) -> None:
    details = load_widget_files(widgets_dir)
    assert len(details) == 32
    assert details == _details()


def test_an_ir_map_in_the_widgets_dir_is_not_a_widget(widgets_dir: Path) -> None:
    shutil.copy(IR_MAP_FIXTURE, widgets_dir / "ir-map.json")
    assert len(load_widget_files(widgets_dir)) == 32


def test_files_that_are_not_json_files_are_ignored(widgets_dir: Path) -> None:
    (widgets_dir / "README.md").write_text("not a widget")
    (widgets_dir / "nested.json").mkdir()
    assert len(load_widget_files(widgets_dir)) == 32


@pytest.mark.parametrize("content", ["{not json", "[]", "{}", '{"id": 7}'])
def test_a_malformed_widget_file_is_a_catalog_error(widgets_dir: Path, content: str) -> None:
    (widgets_dir / "broken.json").write_text(content)
    with pytest.raises(CatalogError, match="broken.json"):
        load_widget_files(widgets_dir)


def test_a_missing_or_empty_widgets_dir_is_a_catalog_error(tmp_path: Path) -> None:
    with pytest.raises(CatalogError, match="no widget files"):
        load_widget_files(tmp_path / "absent")
    (tmp_path / "empty").mkdir()
    with pytest.raises(CatalogError, match="no widget files"):
        load_widget_files(tmp_path / "empty")


# --- the catalog ------------------------------------------------------------


async def test_catalog_from_files_equals_the_catalog_from_the_registry(settings: Settings) -> None:
    backend = Backend()
    async with backend.client(settings) as client:
        catalog = await load_catalog(settings, client)
    assert catalog == build_catalog(_details(), _ir_map())
    assert set(catalog.types) == {*_ir_map(), PLACEHOLDER_TYPE}
    assert catalog.get("pv-label").prop_schema is not None
    assert backend.requests == []


async def test_beaver_is_never_requested_with_a_widgets_dir(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without a client handed in, none is opened either: nothing can reach ``/beaver``."""

    def no_client(*args: object, **kwargs: object):
        raise AssertionError("the backend must not be contacted when CANOPY_WIDGETS_DIR is set")

    monkeypatch.setattr(catalog_mod, "borrowed_client", no_client)
    monkeypatch.setattr(catalog_mod, "fetch_registry", no_client)
    catalog = await ensure_catalog(settings)
    assert len(catalog.types) == len(_ir_map()) + 1


async def test_the_ir_map_still_comes_from_the_viewer_dist(settings: Settings, widgets_dir: Path) -> None:
    (widgets_dir / "ir-map.json").write_text(json.dumps({"only-here": {"consumes": []}}))
    catalog = await load_catalog(settings)
    assert "only-here" not in catalog.types
    with pytest.raises(CatalogError, match="CANOPY_VIEWER_DIST"):
        await load_catalog(settings.model_copy(update={"viewer_dist": None}))


async def test_without_a_widgets_dir_the_registry_is_still_asked(settings: Settings) -> None:
    backend = Backend()
    live = settings.model_copy(update={"widgets_dir": None})
    async with backend.client(live) as client:
        with pytest.raises(CatalogError, match="/beaver/v1/widgets"):
            await load_catalog(live, client)
    assert [request.url.path for request in backend.requests] == ["/beaver/v1/widgets"]


# --- through the app and the tools --------------------------------------------


def test_beaver_404_with_a_widgets_dir_breaks_nothing(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    """Startup loads the catalog from the files while the backend answers 404 to everything."""
    backend = Backend()
    monkeypatch.setattr("canopy_mcp.app.make_client", lambda s: backend.client(s))
    app = create_app(settings, startup_hooks=[catalog_hook])
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
        catalog = catalog_mod._catalog
        assert catalog is not None and len(catalog.types) == len(_ir_map()) + 1
        # FRONT itself has no /beaver either: the prefix is refused, not served.
        assert client.get("/beaver/v1/widgets").status_code in (404, 405)
    assert backend.requests == []


async def test_widget_tools_answer_from_the_files(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CANOPY_VIEWER_DIST", str(settings.viewer_dist))
    monkeypatch.setenv("CANOPY_WIDGETS_DIR", str(settings.widgets_dir))
    monkeypatch.setenv("CANOPY_BASE_URL", "http://127.0.0.1:9")  # nothing listens here
    get_settings.cache_clear()
    try:
        listing = await widget_tools.list_widgets()
        assert listing["total"] == len(_ir_map()) + 1
        widget = await widget_tools.get_widget("pv-label")
        assert widget["type"] == "pv-label"
    finally:
        get_settings.cache_clear()


def test_an_unreadable_widgets_dir_does_not_block_startup(settings: Settings, tmp_path: Path) -> None:
    broken = settings.model_copy(update={"widgets_dir": tmp_path / "absent"})
    app = create_app(broken, startup_hooks=[catalog_hook])
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
    assert catalog_mod._catalog is None
