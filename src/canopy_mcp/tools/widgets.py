"""Catalog tools: which widget types exist, what props they take, and the IR schema.

Without a staged spec the tools still answer: each result carries a structured
``note`` (``{code: "spec_unavailable", message}``) naming what is missing.
"""

import copy
from typing import Any

from fastmcp.exceptions import ToolError

from canopy_mcp.config import get_settings
from canopy_mcp.server import mcp
from canopy_mcp.catalog import ensure_catalog
from canopy_mcp.check.schema import (
    LATEST_VERSION,
    SPEC_UNAVAILABLE,
    SpecUnavailable,
    spec_note,
    load_schema,
)


def _with_spec_note(result: dict[str, Any]) -> dict[str, Any]:
    note = spec_note(get_settings().staged_spec_dir)
    if note is not None:
        result["note"] = note
    return result


@mcp.tool
async def list_widgets() -> dict[str, Any]:
    """List every Screen-IR widget type a screen may use.

    Each entry names the type and flags it: ``undocumented`` types render but
    have no prop documentation; ``placeholder`` marks ``unknown-widget``, which
    stands in for an unconverted legacy widget and should be replaced.
    """
    catalog = await ensure_catalog()
    widgets = [catalog.entries[t].summary() for t in catalog.types]
    return _with_spec_note({"widgets": widgets, "total": len(widgets)})


@mcp.tool
async def get_widget(type: str) -> dict[str, Any]:
    """Describe one widget type: its props, where each prop is documented, and its draft-07 propSchema.

    Each prop lists its ``sources``: ``catalog`` (the staged spec's prop
    schema) and/or ``consumes`` (read by name by the renderer).
    """
    catalog = await ensure_catalog()
    entry = catalog.get(type)
    if entry is None:
        raise ToolError(f"unknown widget type {type!r}; call list_widgets for the valid types")
    return _with_spec_note(entry.to_dict())


@mcp.tool
async def get_schema() -> dict[str, Any]:
    """Return the newest supported Screen-IR JSON Schema (1.1) that screen documents must satisfy.

    Its ``schemaVersion`` enum also accepts 1.0; a document declaring 1.0 is checked
    against the 1.0 schema, which lacks theme colour roles (``$role``). Without a
    staged schema the answer is ``{"schema": null, "note": {code: "spec_unavailable", ...}}``.
    """
    try:
        # A fresh copy: the check pass owns the cached load.
        return copy.deepcopy(load_schema(LATEST_VERSION, get_settings().staged_spec_dir))
    except SpecUnavailable as exc:
        return {"schema": None, "note": {"code": SPEC_UNAVAILABLE, "message": str(exc)}}
