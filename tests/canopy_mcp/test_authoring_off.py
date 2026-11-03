"""``CANOPY_AUTHORING``: off leaves the read-only tools, on offers every tool.

The tools live on one process-wide FastMCP instance, so these tests also pin
that turning authoring off can be repeated and undone, and that the FastMCP
calls it relies on exist in the installed version.
"""

from pathlib import Path

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from canopy_mcp.app import create_app
from canopy_mcp.config import Settings
from canopy_mcp.server import AUTHORING_TOOLS, mcp, set_authoring, authoring_snapshot

READ_TOOLS = [
    "list_widgets",
    "get_widget",
    "get_schema",
    "check",
    "get_screen",
    "list_drafts",
    "search_pvs",
    "read_pv",
    "screenshot",
]


async def tool_names() -> list[str]:
    return sorted(tool.name for tool in await mcp.list_tools())


def build(tmp_path: Path, **overrides: object):
    return create_app(Settings(drafts_dir=tmp_path / "drafts", **overrides), startup_hooks=[])


def test_the_installed_fastmcp_can_remove_and_re_add_a_tool() -> None:
    assert callable(getattr(mcp.local_provider, "remove_tool", None))
    assert callable(getattr(mcp.local_provider, "list_tools", None))
    assert callable(getattr(mcp, "add_tool", None))


def test_authoring_tools_are_the_ones_that_write() -> None:
    assert set(AUTHORING_TOOLS) == {
        "new_screen",
        "patch_node",
        "add_node",
        "remove_node",
        "delete_draft",
        "convert_legacy",
    }
    assert not set(AUTHORING_TOOLS) & set(READ_TOOLS)


def test_authoring_is_on_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CANOPY_AUTHORING", raising=False)
    assert Settings().authoring is True


@pytest.mark.parametrize(("value", "expected"), [("0", False), ("false", False), ("1", True), ("", True)])
def test_authoring_is_read_from_the_environment(monkeypatch: pytest.MonkeyPatch, value: str, expected: bool) -> None:
    monkeypatch.setenv("CANOPY_AUTHORING", value)
    assert Settings().authoring is expected


async def test_app_with_authoring_on_offers_every_tool(tmp_path: Path) -> None:
    build(tmp_path)
    assert await tool_names() == sorted([*READ_TOOLS, *AUTHORING_TOOLS])


async def test_app_with_authoring_off_offers_the_read_tools(tmp_path: Path) -> None:
    build(tmp_path, authoring=False)
    assert await tool_names() == sorted(READ_TOOLS)


async def test_every_tool_is_back_after_an_authoring_off_test() -> None:
    """Runs after the test above: the conftest fixture re-added what that app removed."""
    names = await tool_names()
    assert names == sorted([*READ_TOOLS, *AUTHORING_TOOLS])
    assert set(AUTHORING_TOOLS) <= set(names)


async def test_authoring_off_inside_a_running_loop_and_twice_over(tmp_path: Path) -> None:
    build(tmp_path, authoring=False)
    build(tmp_path, authoring=False)
    assert await tool_names() == sorted(READ_TOOLS)


async def test_a_later_app_with_authoring_on_brings_the_tools_back(tmp_path: Path) -> None:
    before = {tool.name: tool for tool in await mcp.list_tools()}
    build(tmp_path, authoring=False)
    assert set(authoring_snapshot()) == set(AUTHORING_TOOLS)
    build(tmp_path, authoring=True)
    after = {tool.name: tool for tool in await mcp.list_tools()}
    assert sorted(after) == sorted([*READ_TOOLS, *AUTHORING_TOOLS])
    # The same Tool objects, not re-built ones: schema and description are unchanged.
    assert all(after[name] is before[name] for name in AUTHORING_TOOLS)


async def test_turning_authoring_on_twice_adds_nothing_twice(tmp_path: Path) -> None:
    build(tmp_path, authoring=False)
    set_authoring(True)
    set_authoring(True)
    names = await tool_names()
    assert len(names) == len(set(names)) == len(READ_TOOLS) + len(AUTHORING_TOOLS)


async def test_a_client_sees_and_reaches_only_the_read_tools(tmp_path: Path) -> None:
    build(tmp_path, authoring=False)
    async with Client(mcp) as client:
        listed = sorted(tool.name for tool in await client.list_tools())
        assert listed == sorted(READ_TOOLS)
        with pytest.raises(ToolError):
            await client.call_tool("new_screen", {"id": "a/b", "title": "T"})
    assert not (tmp_path / "drafts").exists()  # nothing was written
