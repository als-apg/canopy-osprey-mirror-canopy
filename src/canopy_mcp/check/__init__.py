"""Checks run on a Screen-IR document: schema, semantic (and refs/lint in sibling modules)."""

from canopy_mcp.check.findings import Finding, node_id_at, pointer
from canopy_mcp.check.schema import check_schema, load_schema
from canopy_mcp.channels import WRITE_CAPABLE_TYPES
from canopy_mcp.check.semantic import check_semantic

__all__ = [
    "WRITE_CAPABLE_TYPES",
    "Finding",
    "check_schema",
    "check_semantic",
    "load_schema",
    "node_id_at",
    "pointer",
]
