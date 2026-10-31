"""The MCP tools; importing this package registers every one on ``mcp``.

Each module decorates its tools with ``@mcp.tool`` at import time, so the
imports below exist for that side effect. :func:`canopy_mcp.server.register_all`
imports this package; nothing else needs to. ``drafts`` precedes ``convert``,
which imports it.
"""

from canopy_mcp.tools import pvs, check, drafts, convert, widgets

__all__ = ["check", "convert", "drafts", "pvs", "widgets"]
