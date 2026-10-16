"""The MCP tools; importing this package registers every one on ``mcp``.

Each module decorates its tools with ``@mcp.tool`` at import time, so the
imports below exist for that side effect. :func:`canopy_mcp.server.register_all`
imports this package; nothing else needs to. ``drafts`` precedes ``convert``,
which imports it; ``push`` imports ``check`` itself.
"""

from canopy_mcp.tools import pvs, push, check, drafts, convert, widgets, screenshot, collections

__all__ = ["check", "collections", "convert", "drafts", "push", "pvs", "screenshot", "widgets"]
