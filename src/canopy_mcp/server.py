"""The FastMCP instance and tool registration.

``mcp`` is created at import time so tool modules can decorate against it;
the tool modules themselves are imported only by :func:`register_all`, which
keeps ``import canopy_mcp.server`` cheap and free of side effects.

``mcp`` is one instance per process, so the set of tools it offers is too.
:func:`set_authoring` takes the six :data:`AUTHORING_TOOLS` off it or puts
them back; the app calls it with ``CANOPY_AUTHORING``, and the last app built
in a process decides.
"""

import asyncio
import importlib
from concurrent.futures import ThreadPoolExecutor

from fastmcp import FastMCP
from fastmcp.tools import Tool

mcp = FastMCP("canopy")

_TOOLS_PACKAGE = "canopy_mcp.tools"

# The tools that create, change or delete drafts. Every other tool only reads.
AUTHORING_TOOLS = ("new_screen", "patch_node", "add_node", "remove_node", "delete_draft", "convert_legacy")

# The authoring Tool objects as they were registered, kept from the first
# removal on: the tool modules are imported once, so nothing else can put a
# removed tool back.
_authoring_snapshot: dict[str, Tool] = {}


def register_all() -> FastMCP:
    """Import ``canopy_mcp.tools`` so every tool registers on :data:`mcp`.

    The package imports its tool modules for their registration side effect.
    Until that package exists there is nothing to register; any other import
    failure propagates.
    """
    try:
        importlib.import_module(_TOOLS_PACKAGE)
    except ModuleNotFoundError as exc:
        if exc.name != _TOOLS_PACKAGE:
            raise
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
    return dict(_authoring_snapshot)


def set_authoring(enabled: bool) -> None:
    """Offer the authoring tools on :data:`mcp` (``True``) or take them off it (``False``).

    Turning them off keeps each Tool object before removing it; turning them
    on adds back whichever of those is missing. Both are safe to repeat.
    """
    if enabled and not _authoring_snapshot:
        return  # nothing was ever removed
    registered = registered_tools()
    if enabled:
        for name, tool in _authoring_snapshot.items():
            if name not in registered:
                mcp.add_tool(tool)
        return
    for name in AUTHORING_TOOLS:
        tool = registered.get(name)
        if tool is not None:
            _authoring_snapshot[name] = tool
            mcp.local_provider.remove_tool(name)
