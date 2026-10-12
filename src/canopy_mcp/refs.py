"""Ref keys for ``convert_legacy``: matching a converted parent's refs to legacy source files.

A converted parent names its sub-screens as IR refs (``embedded/_a.screen.json``);
the agent names their sources by the ref as written in the legacy file
(``siblings={'embedded/_a.bob': '/abs/path/_a.bob'}``). Both are reduced to one
KEY and matched on it:

* the RAW string is classified first -- a leading ``/``, ``~`` or ``scheme:`` is
  absolute and has no key (it is never written);
* only a relative string is normalized (``.`` dropped, ``..`` folded as the
  viewer's ``actionHost.normalizePath`` does), then a trailing ``.screen.json``
  (IR ref) or ``.bob|.opi|.edl|.ui`` (``siblings`` key) is stripped.

Where a key's draft lands (``normalize(dirname(parent_id)/key)``) and whether it
climbs above the root is decided by :func:`canopy_mcp.check.refs.resolve_ref`,
after the join. The classification here only decides what is written; the
findings the agent sees come from ``check`` (:mod:`canopy_mcp.check.refs`
applies the same rules).
"""

from __future__ import annotations

from pathlib import PurePosixPath

from canopy_mcp.store import SCREEN_SUFFIX
from canopy_mcp.check.refs import normalize, is_absolute

# Legacy source suffix -> converter kind (``.opi`` goes through the .bob front-end).
SOURCE_KINDS: dict[str, str] = {".bob": "bob", ".opi": "bob", ".edl": "edl", ".ui": "ui"}


def _relative_key(raw: str, suffixes: tuple[str, ...]) -> str | None:
    if not isinstance(raw, str) or is_absolute(raw):
        return None
    joined = "/".join(normalize(raw))
    for suffix in suffixes:
        if joined.lower().endswith(suffix):
            return joined[: -len(suffix)]
    return joined


def ir_ref_key(raw: str) -> str | None:
    """The key of an IR ref as written in a converted document; ``None`` when it is absolute."""
    return _relative_key(raw, (SCREEN_SUFFIX,))


def sibling_key(raw: str) -> str | None:
    """The key of a ``siblings`` entry (a legacy ref as written); ``None`` when it is absolute."""
    return _relative_key(raw, tuple(SOURCE_KINDS))


def source_kind(path: str, default: str) -> str:
    """The converter kind for a legacy source file, from its suffix, else ``default``."""
    return SOURCE_KINDS.get(PurePosixPath(path).suffix.lower(), default)
