"""Every tool is registered on ``mcp`` and the app wires its startup hooks."""

from pathlib import Path
from contextlib import asynccontextmanager

import pytest
from fastmcp.exceptions import ToolError
from starlette.testclient import TestClient

from canopy_mcp import app as app_mod
from canopy_mcp import catalog as catalog_mod
from canopy_mcp.config import Settings
from canopy_mcp.server import mcp, register_all
from canopy_mcp.catalog import CatalogError

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
    "list_collections",
    "delete_draft",
    "convert_legacy",
    "search_pvs",
    "read_pv",
    "screenshot",
]


async def list_tools() -> list[str]:
    return [tool.name for tool in await mcp.list_tools()]


async def test_register_all_registers_every_tool() -> None:
    assert register_all() is mcp
    assert sorted(await list_tools()) == sorted(EXPECTED_TOOLS)


async def test_register_all_is_idempotent() -> None:
    register_all()
    register_all()
    names = await list_tools()
    assert len(names) == len(set(names)) == len(EXPECTED_TOOLS)


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
    calls: list[Settings] = []

    async def fake_load(settings):
        calls.append(settings)
        return sentinel

    monkeypatch.setattr(app_mod, "load_catalog", fake_load)
    settings = Settings(drafts_dir=tmp_path / "drafts")
    app = app_mod.create_app(settings, startup_hooks=[app_mod.catalog_hook])
    with TestClient(app):
        assert calls == [settings]
        assert catalog_mod._catalog is sentinel


def test_catalog_hook_failure_does_not_block_startup(tmp_path: Path, monkeypatch, reset_catalog) -> None:
    async def failing_load(settings):
        raise CatalogError("viewer dist has no ir-map.json")

    monkeypatch.setattr(app_mod, "load_catalog", failing_load)
    app = app_mod.create_app(Settings(drafts_dir=tmp_path / "drafts"), startup_hooks=[app_mod.catalog_hook])
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
    assert catalog_mod._catalog is None  # left for ensure_catalog to retry on first use


def test_chromium_hook_installs_and_stops_the_browser(tmp_path: Path, monkeypatch) -> None:
    """The hook is entered with the server's own URL, no credential and the sandbox off, and exited on shutdown."""
    events: list[str] = []

    def fake_browser_hook(base_url: str, **kwargs):
        @asynccontextmanager
        async def hook(app):
            events.append(f"start {base_url} {sorted(kwargs.items())}")
            yield
            events.append("stop")

        return hook

    monkeypatch.setattr(app_mod, "browser_hook", fake_browser_hook)
    app = app_mod.create_app(
        Settings(drafts_dir=tmp_path / "drafts"),
        startup_hooks=[app_mod.chromium_hook],
        self_url="http://127.0.0.1:9999",
    )
    started = "start http://127.0.0.1:9999 [('access_token', None), ('sandbox', False)]"
    with TestClient(app):
        assert events == [started]
    assert events == [started, "stop"]


def test_chromium_hook_carries_the_credential_and_the_sandbox_switch(tmp_path: Path, monkeypatch) -> None:
    seen: list[tuple[str, dict]] = []

    def fake_browser_hook(base_url: str, **kwargs):
        @asynccontextmanager
        async def hook(app):
            seen.append((base_url, kwargs))
            yield

        return hook

    monkeypatch.setattr(app_mod, "browser_hook", fake_browser_hook)
    token_file = tmp_path / "front.access"
    token_file.write_text("registry-token-7\n")
    settings = Settings(drafts_dir=tmp_path / "drafts", access_token_file=token_file, browser_sandbox=True)
    app = app_mod.create_app(settings, startup_hooks=[app_mod.chromium_hook], self_url="http://127.0.0.1:9998")
    with TestClient(app):
        pass
    assert seen == [("http://127.0.0.1:9998", {"access_token": "registry-token-7", "sandbox": True})]


def test_chromium_hook_launches_nothing_and_a_failed_launch_does_not_block_startup(tmp_path: Path, monkeypatch) -> None:
    """The hook installs the browser without launching; a host whose Chromium cannot launch still serves."""
    from fastmcp import Client

    from canopy_mcp import browser as browser_mod
    from canopy_mcp.server import mcp

    launches: list[bool] = []

    async def no_chromium(playwright, *, sandbox: bool) -> None:
        launches.append(sandbox)
        raise RuntimeError("Executable doesn't exist; run `playwright install chromium`")

    monkeypatch.setattr(browser_mod, "_launch_chromium", no_chromium)
    app = app_mod.create_app(Settings(drafts_dir=tmp_path / "drafts"), startup_hooks=[app_mod.chromium_hook])
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
        browser = app.state.browser
        assert isinstance(browser, browser_mod.Browser)
        assert browser_mod.get_browser() is browser
        assert launches == [] and not browser.running

        async def call() -> None:
            async with Client(mcp) as mcp_client:
                await mcp_client.call_tool("screenshot", {"id": "xfer/view"})

        with pytest.raises(ToolError, match="unavailable"):
            client.portal.call(call)
        assert launches == [False] and not browser.running
    with pytest.raises(browser_mod.BrowserNotStarted):
        browser_mod.get_browser()


def test_self_url_defaults_to_the_launcher_address(tmp_path: Path) -> None:
    from canopy_mcp.__main__ import DEFAULT_HOST, DEFAULT_PORT

    app = app_mod.create_app(Settings(drafts_dir=tmp_path / "drafts"), startup_hooks=[])
    assert app.state.self_url == f"http://{DEFAULT_HOST}:{DEFAULT_PORT}"
