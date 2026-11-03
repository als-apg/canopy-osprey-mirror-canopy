"""The FastMCP instance and tool registration.

``mcp`` is created at import time so tool modules can decorate against it;
the tool modules themselves are imported only by :func:`register_all`, which
keeps ``import canopy_mcp.server`` cheap and free of side effects.

``mcp`` is one instance per process, so the set of tools it offers is too.
:func:`set_authoring` takes the :data:`AUTHORING_TOOLS` off it or puts them
back; the app calls it with ``CANOPY_AUTHORING``. :func:`set_workspace_tools`
does the same for the :data:`WORKSPACE_TOOLS`, which read and change the
CANOPY workspace (not drafts, so they are not authoring tools); the app calls it
with ``CANOPY_WORKSPACE``. Every tool registers at import, so the full set is
known without building an app; the last app built in a process decides which of
them it offers.
"""

import asyncio
import importlib
from concurrent.futures import ThreadPoolExecutor

from fastmcp import FastMCP
from fastmcp.tools import Tool

mcp = FastMCP(
    "canopy",
    instructions="Build Canopy screens by the canopy-panel-builder skill and operate the workspace by the "
    "canopy-operator skill (.claude/skills/<name>/SKILL.md; `python -m canopy_mcp.skill install <project dir>` "
    "writes both).",
)

_TOOLS_PACKAGE = "canopy_mcp.tools"

# The tools that create, change or delete drafts. Every other tool only reads.
AUTHORING_TOOLS = (
    "new_screen",
    "patch_node",
    "add_node",
    "remove_node",
    "delete_draft",
    "convert_legacy",
)

# The tools that read and change the CANOPY workspace through the in-process hub
# (canopy_mcp.workspace). They neither read nor write drafts.
WORKSPACE_TOOLS = (
    "get_workspace",
    "set_workspace",
)

# The Tool objects taken off ``mcp``, by group and name, kept from the first removal
# on: the tool modules are imported once, so nothing else can put a removed tool back.
_removed: dict[str, dict[str, Tool]] = {"authoring": {}, "workspace": {}}


def register_all() -> FastMCP:
    """Import ``canopy_mcp.tools`` so every tool registers on :data:`mcp`.

    The package imports its tool modules for their registration side effect.
    """
    importlib.import_module(_TOOLS_PACKAGE)
    return mcp


def registered_tools() -> dict[str, Tool]:
    """The tools on :data:`mcp` by name, read without needing the caller's event loop.

    FastMCP only lists tools through a coroutine, and the app is built by
    synchronous code that may itself be running inside a loop, so the listing
    runs on a loop of its own in a worker thread.
    """
    with ThreadPoolExecutor(max_workers=1) as pool:
        tools = pool.submit(asyncio.run, mcp.local_provider.list_tools()).result()
    return {tool.name: tool for tool in tools}


def authoring_snapshot() -> dict[str, Tool]:
    """The authoring tools removed so far, by name (empty until authoring was first turned off)."""
    return dict(_removed["authoring"])


def workspace_snapshot() -> dict[str, Tool]:
    """The workspace tools removed so far, by name (empty until they were first turned off)."""
    return dict(_removed["workspace"])


def _offer(group: str, names: tuple[str, ...], enabled: bool) -> None:
    """Put the tools ``names`` on :data:`mcp` (``True``) or take them off it (``False``).

    Turning them off keeps each Tool object under ``group`` before removing it;
    turning them on adds back whichever of those is missing. Both are safe to repeat.
    """
    removed = _removed[group]
    if enabled and not removed:
        return  # nothing was ever removed
    registered = registered_tools()
    if enabled:
        for name, tool in removed.items():
            if name not in registered:
                mcp.add_tool(tool)
        return
    for name in names:
        tool = registered.get(name)
        if tool is not None:
            removed[name] = tool
            mcp.local_provider.remove_tool(name)


def set_authoring(enabled: bool) -> None:
    """Offer :data:`AUTHORING_TOOLS` on :data:`mcp` (``True``) or take them off it (``False``)."""
    _offer("authoring", AUTHORING_TOOLS, enabled)


def set_workspace_tools(enabled: bool) -> None:
    """Offer :data:`WORKSPACE_TOOLS` on :data:`mcp` (``True``) or take them off it (``False``).

    The same rules as :func:`set_authoring`, over its own group, so either switch
    leaves the other's tools alone.
    """
    _offer("workspace", WORKSPACE_TOOLS, enabled)
