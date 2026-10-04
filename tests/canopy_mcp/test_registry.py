"""Every tool is registered on ``mcp`` and the app wires its startup hooks."""

from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest
from starlette.testclient import TestClient

from canopy_mcp import app as app_mod
from canopy_mcp import catalog as catalog_mod
from canopy_mcp.config import Settings
from canopy_mcp.server import mcp, register_all

EXPECTED_TOOLS = [
    "list_widgets",
    "get_widget",
    "get_schema",
    "check",
    "new_screen",
    "get_screen",
    "patch_node",
    "add_node",
    "remove_node",
    "list_drafts",
    "delete_draft",
    "convert_legacy",
    "search_pvs",
    "read_pv",
    "screenshot",
]


async def list_tools() -> list[str]:
    return [tool.name for tool in await mcp.list_tools()]


async def test_register_all_registers_all_fifteen_tools() -> None:
    assert register_all() is mcp
    assert sorted(await list_tools()) == sorted(EXPECTED_TOOLS)


async def test_register_all_is_idempotent() -> None:
    register_all()
    register_all()
    names = await list_tools()
    assert len(names) == len(set(names)) == 15


async def test_building_the_app_registers_the_tools() -> None:
    app_mod.create_app(Settings(drafts_dir=Path("unused")), startup_hooks=[])
    assert sorted(await list_tools()) == sorted(EXPECTED_TOOLS)


def test_default_startup_hooks_are_catalog_then_browser() -> None:
    assert app_mod.STARTUP_HOOKS == [app_mod.catalog_hook, app_mod.chromium_hook]


@pytest.fixture
def reset_catalog():
    catalog_mod.set_catalog(None)
    yield
    catalog_mod.set_catalog(None)


def test_catalog_hook_loads_the_catalog_at_startup(tmp_path: Path, monkeypatch, reset_catalog) -> None:
    sentinel = object()
    calls: list[tuple[Settings, httpx.AsyncClient]] = []

    async def fake_load(settings, client):
        calls.append((settings, client))
        return sentinel

    monkeypatch.setattr(app_mod, "load_catalog", fake_load)
    settings = Settings(drafts_dir=tmp_path / "drafts")
    app = app_mod.create_app(settings, startup_hooks=[app_mod.catalog_hook])
    with TestClient(app):
        assert calls == [(settings, app.state.http)]
        assert catalog_mod._catalog is sentinel


def test_catalog_hook_failure_does_not_block_startup(tmp_path: Path, monkeypatch, reset_catalog) -> None:
    async def failing_load(settings, client):
        raise httpx.ConnectError("backend down")

    monkeypatch.setattr(app_mod, "load_catalog", failing_load)
    app = app_mod.create_app(Settings(drafts_dir=tmp_path / "drafts"), startup_hooks=[app_mod.catalog_hook])
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
    assert catalog_mod._catalog is None  # left for ensure_catalog to retry on first use


def test_chromium_hook_starts_and_stops_the_browser(tmp_path: Path, monkeypatch) -> None:
    events: list[str] = []

    def fake_browser_hook(base_url: str):
        @asynccontextmanager
        async def hook(app):
            events.append(f"start {base_url}")
            yield
            events.append("stop")

        return hook

    monkeypatch.setattr(app_mod, "browser_hook", fake_browser_hook)
    app = app_mod.create_app(
        Settings(drafts_dir=tmp_path / "drafts"),
        startup_hooks=[app_mod.chromium_hook],
        self_url="http://127.0.0.1:9999",
    )
    with TestClient(app):
        assert events == ["start http://127.0.0.1:9999"]
    assert events == ["start http://127.0.0.1:9999", "stop"]


def test_chromium_hook_without_a_browser_does_not_block_startup(tmp_path: Path, monkeypatch) -> None:
    from canopy_mcp import browser as browser_mod

    async def no_chromium(self) -> None:
        raise RuntimeError("Executable doesn't exist; run `playwright install chromium`")

    monkeypatch.setattr(browser_mod.Browser, "start", no_chromium)
    app = app_mod.create_app(Settings(drafts_dir=tmp_path / "drafts"), startup_hooks=[app_mod.chromium_hook])
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
        assert app.state.browser is None
        with pytest.raises(browser_mod.BrowserNotStarted):
            browser_mod.get_browser()


def test_self_url_defaults_to_the_launcher_address(tmp_path: Path) -> None:
    from canopy_mcp.__main__ import DEFAULT_HOST, DEFAULT_PORT

    app = app_mod.create_app(Settings(drafts_dir=tmp_path / "drafts"), startup_hooks=[])
    assert app.state.self_url == f"http://{DEFAULT_HOST}:{DEFAULT_PORT}"
