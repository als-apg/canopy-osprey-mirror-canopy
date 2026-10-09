"""Structural check: validate a document against the staged Screen-IR schema.

The IR schemas are not part of any package: they come from the spec staging
directory (``Settings.staged_spec_dir`` -- ``CANOPY_SPEC_DIR``, else ``spec/``
beside the viewer dist), one file per version at
``ir/screen-ir-<version>.schema.json``. A document is validated against the
schema of the version it declares: a 1.0 document must not use 1.1 constructs
(theme colour roles), while a 1.1 document may. A document declaring an
unsupported (or no) version is validated against the newest schema, whose
``schemaVersion`` enum then reports the bad version at ``/schemaVersion``
alongside every other structural error.

Without a staging directory, or without the schema for the version a document
declares, the pass returns exactly one warning ``spec_unavailable`` naming what
is missing, never a silent pass.
"""

from __future__ import annotations

import json
from typing import Any
from pathlib import Path
from functools import lru_cache

from jsonschema import Draft202012Validator

from canopy_mcp.check.findings import Finding, pointer, node_id_at

SUPPORTED_VERSIONS: tuple[str, ...] = ("1.0", "1.1")
LATEST_VERSION = SUPPORTED_VERSIONS[-1]
SCHEMA_CODE = "schema"
SPEC_UNAVAILABLE = "spec_unavailable"
IR_SCHEMA_DIR = "ir"


class SpecUnavailable(LookupError):
    """The staged spec cannot supply what was asked; ``str()`` names what is missing."""


def schema_path(spec_dir: Path, version: str) -> Path:
    """Where the staged IR schema of ``version`` lives under ``spec_dir``."""
    return Path(spec_dir) / IR_SCHEMA_DIR / f"screen-ir-{version}.schema.json"


def spec_dir_problem(spec_dir: Path | None) -> str | None:
    """Why ``spec_dir`` is not a usable staging directory, or ``None`` when it is one."""
    if spec_dir is None:
        return "no spec staging directory: CANOPY_SPEC_DIR and CANOPY_VIEWER_DIST are unset"
    if not Path(spec_dir).is_dir():
        return f"no spec staging directory at {spec_dir}"
    return None


def spec_note(spec_dir: Path | None, version: str | None = None) -> dict[str, str] | None:
    """The structured ``spec_unavailable`` note a tool answers with, or ``None`` when the spec is there.

    With ``version``, the IR schema of that version must be staged too.
    """
    try:
        if version is None:
            problem = spec_dir_problem(spec_dir)
            if problem is not None:
                raise SpecUnavailable(problem)
        else:
            load_schema(version, spec_dir)
    except SpecUnavailable as exc:
        return {"code": SPEC_UNAVAILABLE, "message": str(exc)}
    return None


def load_schema(version: str = LATEST_VERSION, spec_dir: Path | None = None) -> dict[str, Any]:
    """The staged Screen-IR JSON Schema for ``version`` -- the newest supported one by default.

    Cached per file and modification time; treat the result as read-only.
    Raises :class:`SpecUnavailable` when the staging directory or the file is
    missing or unreadable.
    """
    return _load(*_locate(version, spec_dir))


def _locate(version: str, spec_dir: Path | None) -> tuple[str, int]:
    """``(path, mtime_ns)`` of the staged schema of ``version``; the cache key of its load."""
    problem = spec_dir_problem(spec_dir)
    if problem is not None:
        raise SpecUnavailable(problem)
    path = schema_path(spec_dir, version)  # type: ignore[arg-type]
    try:
        return str(path), path.stat().st_mtime_ns
    except OSError:
        raise SpecUnavailable(f"no IR schema for schemaVersion {version}: {path} is missing") from None


@lru_cache(maxsize=16)
def _load(path: str, mtime_ns: int) -> dict[str, Any]:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SpecUnavailable(f"cannot read the IR schema {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise SpecUnavailable(f"the IR schema {path} is not a JSON object")
    return data


@lru_cache(maxsize=16)
def _validator(path: str, mtime_ns: int) -> Draft202012Validator:
    return Draft202012Validator(_load(path, mtime_ns))


def schema_version_of(doc: Any) -> str:
    """The supported version ``doc`` declares, or the newest one when it declares none we support."""
    declared = doc.get("schemaVersion") if isinstance(doc, dict) else None
    if isinstance(declared, str) and declared in SUPPORTED_VERSIONS:
        return declared
    return LATEST_VERSION


def spec_unavailable_finding(message: str) -> Finding:
    """The one warning a check returns when the staged spec cannot validate the document."""
    return Finding(node_id=None, code=SPEC_UNAVAILABLE, path=pointer(()), severity="warning", message=message)


def check_schema(doc: Any, spec_dir: Path | None) -> list[Finding]:
    """One ``schema`` error per jsonschema error, mapped to the nearest node id.

    The document is validated against the staged schema of the version it
    declares (see :func:`schema_version_of`). Findings are sorted by path, then
    message, so repeated runs are identical. Without that schema the result is
    exactly one ``spec_unavailable`` warning.
    """
    try:
        located = _locate(schema_version_of(doc), spec_dir)
        validator = _validator(*located)
    except SpecUnavailable as exc:
        return [spec_unavailable_finding(str(exc))]
    findings = []
    for error in validator.iter_errors(doc):
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
