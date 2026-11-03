"""The MCP tools; importing this package registers every one on ``mcp``.

Each module decorates its tools with ``@mcp.tool`` at import time, so the
imports below exist for that side effect. :func:`canopy_mcp.server.register_all`
imports this package; nothing else needs to. ``drafts`` precedes ``convert``,
which imports it.

The shared library's tool (``list_collections``) is registered by the second
import alone; the authoring tools above it never import it.
"""

from canopy_mcp.tools import pvs, check, drafts, convert, widgets, screenshot
from canopy_mcp.tools import collections

__all__ = ["check", "collections", "convert", "drafts", "pvs", "screenshot", "widgets"]
