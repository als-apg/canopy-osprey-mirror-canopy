"""The MCP tools; importing this package registers every one on ``mcp``.

Each module decorates its tools with ``@mcp.tool`` at import time, so the
imports below exist for that side effect. :func:`canopy_mcp.server.register_all`
imports this package; nothing else needs to. ``drafts`` precedes ``convert``,
which imports it.

The shared library's tools (``list_collections`` and the workspace hub's
``get_workspace``/``set_workspace``) are registered by the second import alone;
the authoring tools above it never import them.
"""

from canopy_mcp.tools import pvs, check, drafts, convert, widgets, screenshot
from canopy_mcp.tools import workspace, collections

__all__ = ["check", "collections", "convert", "drafts", "pvs", "screenshot", "widgets", "workspace"]
