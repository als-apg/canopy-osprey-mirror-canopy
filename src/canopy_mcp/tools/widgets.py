"""Catalog tools: which widget types exist, what props they take, and the IR schema."""

from typing import Any

from fastmcp.exceptions import ToolError

from canopy_mcp.server import mcp
from canopy_mcp.catalog import load_schema, ensure_catalog


@mcp.tool
async def list_widgets() -> dict[str, Any]:
    """List every Screen-IR widget type a screen may use.

    Each entry names the type and flags it: ``undocumented`` types render but
    have no prop documentation; ``placeholder`` marks ``unknown-widget``, which
    stands in for an unconverted legacy widget and should be replaced.
    """
    catalog = await ensure_catalog()
    widgets = [catalog.entries[t].summary() for t in catalog.types]
    return {"widgets": widgets, "total": len(widgets)}


@mcp.tool
async def get_widget(type: str) -> dict[str, Any]:
    """Describe one widget type: its props, where each prop is documented, and its draft-07 propSchema.

    Each prop lists its ``sources``: ``beaver`` (the widget registry's
    propSchema) and/or ``consumes`` (read by name by the renderer).
    """
    catalog = await ensure_catalog()
    entry = catalog.get(type)
    if entry is None:
        raise ToolError(f"unknown widget type {type!r}; call list_widgets for the valid types")
    return entry.to_dict()


@mcp.tool
async def get_schema() -> dict[str, Any]:
    """Return the Screen-IR 1.0 JSON Schema every screen document must satisfy."""
    return load_schema()
