"""Structural check: validate a document against the vendored Screen-IR schema.

Each supported ``schemaVersion`` has its own vendored schema file, and a
document is validated against the schema of the version it declares: a 1.0
document must not use 1.1 constructs (theme colour roles), while a 1.1 document
may. A document declaring an unsupported (or no) version is validated against
the newest schema, whose ``schemaVersion`` enum then reports the bad version at
``/schemaVersion`` alongside every other structural error.

The schemas are read through ``importlib.resources`` by package *name* — the
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
SCHEMA_FILES = {
    "1.0": "screen-ir-1.0.schema.json",
    "1.1": "screen-ir-1.1.schema.json",
}
SUPPORTED_VERSIONS = tuple(sorted(SCHEMA_FILES, key=lambda v: tuple(int(p) for p in v.split("."))))
LATEST_VERSION = SUPPORTED_VERSIONS[-1]
SCHEMA_FILE = SCHEMA_FILES[LATEST_VERSION]
SCHEMA_CODE = "schema"


def load_schema(version: str = LATEST_VERSION) -> dict[str, Any]:
    """The vendored Screen-IR JSON Schema for ``version`` — the newest supported one by default.

    Cached; treat the result as read-only. Raises :class:`KeyError` for an
    unsupported version.
    """
    return _load(version)


@lru_cache(maxsize=len(SCHEMA_FILES))
def _load(version: str) -> dict[str, Any]:
    name = SCHEMA_FILES[version]
    text = (files(SCHEMA_PACKAGE) / "schemas" / name).read_text(encoding="utf-8")
    return json.loads(text)


@lru_cache(maxsize=len(SCHEMA_FILES))
def _validator(version: str) -> Draft202012Validator:
    return Draft202012Validator(load_schema(version))


def schema_version_of(doc: Any) -> str:
    """The supported version ``doc`` declares, or the newest one when it declares none we support."""
    declared = doc.get("schemaVersion") if isinstance(doc, dict) else None
    if isinstance(declared, str) and declared in SCHEMA_FILES:
        return declared
    return LATEST_VERSION


def check_schema(doc: Any) -> list[Finding]:
    """One ``schema`` error per jsonschema error, mapped to the nearest node id.

    The document is validated against the schema of the version it declares
    (see :func:`schema_version_of`). Findings are sorted by path, then message,
    so repeated runs are identical.
    """
    findings = []
    for error in _validator(schema_version_of(doc)).iter_errors(doc):
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
