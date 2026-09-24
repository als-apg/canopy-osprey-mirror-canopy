"""Structural check: validate a document against the vendored Screen-IR schema.

The schema is read through ``importlib.resources`` by package *name* — the
import-linter contract forbids ``canopy_mcp`` from importing ``canopy.beaver``.
"""

from __future__ import annotations

import json
from functools import lru_cache
from importlib.resources import files
from typing import Any

from jsonschema import Draft202012Validator

from canopy_mcp.check.findings import Finding, node_id_at, pointer

SCHEMA_PACKAGE = "canopy.beaver.data"
SCHEMA_FILE = "screen-ir-1.0.schema.json"
SCHEMA_CODE = "schema"


@lru_cache(maxsize=1)
def load_schema() -> dict[str, Any]:
    """The vendored Screen-IR 1.0 JSON Schema (cached; treat as read-only)."""
    text = (files(SCHEMA_PACKAGE) / "schemas" / SCHEMA_FILE).read_text(encoding="utf-8")
    return json.loads(text)


@lru_cache(maxsize=1)
def _validator() -> Draft202012Validator:
    return Draft202012Validator(load_schema())


def check_schema(doc: Any) -> list[Finding]:
    """One ``schema`` error per jsonschema error, mapped to the nearest node id.

    Findings are sorted by path, then message, so repeated runs are identical.
    """
    findings = []
    for error in _validator().iter_errors(doc):
        parts = list(error.absolute_path)
        findings.append(
            Finding(
                node_id=node_id_at(doc, parts),
                code=SCHEMA_CODE,
                path=pointer(parts),
                severity="error",
                message=error.message,
            )
        )
    findings.sort(key=lambda f: (f.path, f.message))
    return findings
