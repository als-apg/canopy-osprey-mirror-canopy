"""The FastMCP instance and tool registration.

``mcp`` is created at import time so tool modules can decorate against it;
the tool modules themselves are imported only by :func:`register_all`, which
keeps ``import canopy_mcp.server`` cheap and free of side effects.
"""

import importlib

from fastmcp import FastMCP

mcp = FastMCP("canopy")

_TOOLS_PACKAGE = "canopy_mcp.tools"


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
