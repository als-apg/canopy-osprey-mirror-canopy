"""``python -m canopy_mcp.collections <dir> --check [--library <root>]``.

Checks a facility collection directory the way the server would read it, and
refuses what the server would silently skip. Every ``*.json`` directly in
``<dir>`` (in name order) is one checked file: its stem must be a collection id,
its bytes UTF-8 JSON, and the document must pass
:func:`canopy_mcp.collections.validate_collection` as a facility file with the stem
as its id. With ``--library``, every ``library/<id>`` screen member must be a screen
that library root lists (a missing root lists none); the root is scanned once.

Output: one ``canopy_mcp.collections: refused: <file>: <rule>`` line on stderr per
refused file, then ``collections: <N> checked, <M> refused`` on stdout. Exit
:data:`EXIT_OK` when nothing is refused (a missing ``<dir>`` checks nothing),
:data:`EXIT_REFUSED` on any refusal, :data:`EXIT_ERROR` when ``<dir>`` or the
library cannot be read. Nothing is written, so it runs under ``python -I`` from a
read-only deployment tree; this is what OSPREY's image check and contract lane call.
"""

from __future__ import annotations

import sys
import json
import argparse
from pathlib import Path
from collections.abc import Sequence

from canopy_mcp.views import VIEW_ID_PATTERN
from canopy_mcp.listing import Judged, scan, judge
from canopy_mcp.collections import (
    FACILITY,
    COLLECTION_SUFFIX,
    InvalidCollection,
    validate_collection,
)

__all__ = ["EXIT_OK", "EXIT_ERROR", "EXIT_REFUSED", "check", "main", "parse_args"]

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_REFUSED = 3
PROG = "python -m canopy_mcp.collections"


def _printable(value: object) -> str:
    """``value`` on one ASCII line: control and non-ASCII characters as backslash escapes."""
    return str(value).encode("unicode_escape").decode("ascii")


def _say(message: str) -> None:
    print(f"canopy_mcp.collections: {message}", file=sys.stderr)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog=PROG, description="Check a facility collection directory.")
    parser.add_argument("dir", type=Path, help="the facility collection directory")
    parser.add_argument("--check", action="store_true", required=True, help="validate every *.json in the directory")
    parser.add_argument("--library", type=Path, help="the library store root that library/* screen members must be in")
    return parser.parse_args(argv)


def _library(root: Path) -> Judged:
    if not root.exists():
        return Judged(files=(), refusals=())
    if not root.is_dir():
        raise NotADirectoryError(f"the library {_printable(root)} is not a directory")
    return judge(scan(root))


def _refusal(path: Path, library: Judged | None) -> str | None:
    """Why the file at ``path`` is refused, or ``None`` when it passes."""
    stem = path.name[: -len(COLLECTION_SUFFIX)]
    if not VIEW_ID_PATTERN.fullmatch(stem):
        return "the file name is not a collection id (^[A-Za-z0-9_-]{1,64}$ plus .json)"
    if not path.is_file():
        return "not a regular file"
    try:
        doc = json.loads(path.read_bytes().decode("utf-8"))
    except UnicodeDecodeError:
        return "not UTF-8"
    except (ValueError, RecursionError) as exc:
        return f"not JSON: {exc}"
    try:
        validate_collection(doc, source=FACILITY, library=library, file_id=stem)
    except InvalidCollection as exc:
        return str(exc)
    return None


def check(directory: Path, library_root: Path | None = None) -> int:
    """Run the check on ``directory``; prints as described in the module text."""
    try:
        library = _library(library_root) if library_root is not None else None
        if directory.exists() and not directory.is_dir():
            raise NotADirectoryError(f"{_printable(directory)} is not a directory")
        paths = (
            sorted((p for p in directory.iterdir() if p.name.endswith(COLLECTION_SUFFIX)), key=lambda p: p.name)
            if directory.exists()
            else []
        )
        refusals = [(path, reason) for path in paths if (reason := _refusal(path, library)) is not None]
    except OSError as exc:
        _say(f"cannot read: {_printable(exc)}")
        return EXIT_ERROR
    for path, reason in refusals:
        _say(f"refused: {_printable(path.name)}: {_printable(reason)}")
    print(f"collections: {len(paths)} checked, {len(refusals)} refused")
    return EXIT_REFUSED if refusals else EXIT_OK


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    return check(args.dir, args.library)
