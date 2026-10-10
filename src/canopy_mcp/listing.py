"""The listing of a screen library: which files it holds, their digest, and what does not belong.

A library root holds ``screens/`` and, beside it, the two report files
``WRITE-BINDINGS.json`` and ``WRITE-BINDINGS.md``. The listing is the one
definition of "the files of the library": the report is computed over it, the
image build recomputes it, and a deployment compares its digest to tell a stale
report. It is two functions:

- :func:`scan` reads the filesystem and says what is there, nothing more: one
  entry ``{path, kind, mode, sha256}`` per directory entry, with ``kind`` one of
  ``file``, ``dir``, ``symlink`` or ``other`` (from ``lstat``, so a symlink is
  never followed) and ``sha256`` on regular files only.
- :func:`judge` is pure. From entries alone it gives the list, its digest and
  the refusals. It never asks the filesystem, so its answer is the same on a
  host that cannot hold two names differing only in case; the shared vectors
  (``listing-vectors.json``) are entry lists run through it.

**The list.** One line ``<sha256 hex>  screens/<relpath>\\n`` (two spaces) per
listed file, sorted by the UTF-8 bytes of ``relpath``, spaces kept and nothing
escaped, encoded as UTF-8. The digest is ``sha256:`` and the hex sha256 of those
bytes. A file is listed when it is a regular file under ``screens/`` named
``*.screen.json`` (as spelled) or carrying an image suffix (in any case), and no
segment of its path is refused.

**Refusals.** Every other entry is refused by path and code, and the text of a
refusal says what to do with it:

- ``outside_screens``: an entry of the root other than ``screens/`` and the two
  report files (reported once, by its top-level name).
- ``symlink``, ``not_regular``: a symlink anywhere; a socket, pipe or device.
- ``bad_name``: a name holding a control, format or surrogate character, which
  the list could not carry on one line.
- ``leftover_tmp``: a ``.*.tmp`` name, what an interrupted atomic write leaves,
  under ``screens/`` or in the root itself.
- ``dotfile``: any other name starting with ``.``.
- ``ignored_name``: a name the image build context or the deployment's
  ``.gitignore`` prunes (:func:`is_ignored_name`), so the image would not hold it.
- ``suffix``: a regular file that is neither a screen nor an image.
- ``case_collision``: entries, or directory names under one parent, that are
  equal under ``str.casefold()``.

A name rule applies to every segment below ``screens/`` and is reported at the
shortest path that breaks it, so a directory named ``img.log`` is one refusal,
whatever it holds. One exception: a regular file named exactly ``.DS_Store`` is
skipped, neither listed nor refused, since git ignores it and the image context
drops it.

Stdlib only, besides ``canopy_mcp.store`` for the suffix sets and the two
predicates.
"""

from __future__ import annotations

import os
import stat
import hashlib
import unicodedata
from typing import TypedDict, NotRequired
from pathlib import Path
from dataclasses import dataclass
from collections.abc import Iterable

from canopy_mcp.store import (
    SCREEN_SUFFIX,
    IMAGE_SUFFIXES,
    RESERVED_ENDINGS,
    RESERVED_SEGMENTS,
)

__all__ = [
    "REPORT_NAMES",
    "SCREENS_DIRNAME",
    "Entry",
    "Judged",
    "Refusal",
    "is_ignored_name",
    "judge",
    "library_digest",
    "refusal_text",
    "scan",
]

SCREENS_DIRNAME = "screens"
#: The only entries a library root may hold beside ``screens/``.
REPORT_NAMES = ("WRITE-BINDINGS.json", "WRITE-BINDINGS.md")
#: The one name that is skipped: neither listed nor refused.
SKIPPED_NAME = ".DS_Store"

_NAME_CATEGORIES = frozenset({"Cc", "Cf", "Cs"})
_REASONS = {
    "outside_screens": "a library root holds only screens/ and the WRITE-BINDINGS files; delete it",
    "symlink": "a symlink; delete it, or replace it with the file it points at",
    "not_regular": "not a regular file; delete it",
    "bad_name": "its name holds a control or format character; delete or rename it",
    "leftover_tmp": "a leftover of an interrupted write; delete it",
    "dotfile": "a dotfile; delete it",
    "ignored_name": "a name the image build leaves out; delete or rename it",
    "suffix": "neither a *.screen.json file nor an image; delete it",
    "case_collision": "differs from another entry only in case; delete or rename one of them",
}


class Entry(TypedDict):
    """One directory entry as :func:`scan` saw it; ``path`` is relative to the root, with ``/``."""

    path: str
    kind: str
    mode: int
    sha256: NotRequired[str]


class Refusal(TypedDict):
    """One entry that does not belong in a library, and why (a key of the module text's list)."""

    path: str
    code: str


@dataclass(frozen=True)
class Judged:
    """What :func:`judge` makes of a root's entries.

    ``files`` holds ``(store path, sha256)`` per listed file in list order;
    ``refusals`` is sorted by path and code.
    """

    files: tuple[tuple[str, str], ...]
    refusals: tuple[Refusal, ...]

    @property
    def list_bytes(self) -> bytes:
        """The list: one line ``<sha256>  screens/<relpath>`` per file."""
        return "".join(f"{sha}  {path}\n" for path, sha in self.files).encode("utf-8")

    @property
    def digest(self) -> str:
        """The digest of :attr:`list_bytes`."""
        return library_digest(self.list_bytes)

    @property
    def paths(self) -> frozenset[str]:
        """The listed store paths, each exactly as spelled on disk."""
        return frozenset(path for path, _sha in self.files)


def library_digest(listing: bytes) -> str:
    """``sha256:`` + the hex sha256 of the list's bytes."""
    return f"sha256:{hashlib.sha256(listing).hexdigest()}"


def is_ignored_name(segment: str) -> bool:
    """Whether a file or directory called ``segment`` is left out of the image build context.

    True for a name starting with ``.``, for ``var``, ``__pycache__``,
    ``prefetched-models``, ``CLAUDE.local.md`` and ``Dockerfile``, and for a name
    ending in ``.swp``, ``.swo``, ``.pyc``, ``.pyo``, ``.pyd``, ``.log`` or
    ``.pid``, each compared on ``segment.casefold()``. These are the store's
    reserved names without its leading-``-`` rule, which keeps ids clear of
    command-line options and has nothing to do with what a build copies.
    """
    folded = segment.casefold()
    return folded.startswith(".") or folded in RESERVED_SEGMENTS or folded.endswith(RESERVED_ENDINGS)


def refusal_text(refusal: Refusal) -> str:
    """One printable line for a refusal: its code, its path (escaped to ASCII) and what to do."""
    path = refusal["path"].encode("unicode_escape").decode("ascii")
    return f"{refusal['code']}: {path}: {_REASONS[refusal['code']]}"


# --- scan -----------------------------------------------------------------------------


def _kind(mode: int) -> str:
    if stat.S_ISLNK(mode):
        return "symlink"
    if stat.S_ISDIR(mode):
        return "dir"
    return "file" if stat.S_ISREG(mode) else "other"


def scan(root: Path | str) -> list[Entry]:
    """Every entry of the library root ``root``, in a stable order (each directory sorted by name).

    The root's own entries are all reported; of its directories only
    ``screens/`` is descended into, so a stray directory beside it is one entry
    however much it holds. Symlinks are reported, never followed. ``mode`` is the
    permission bits. Raises :class:`OSError` when ``root`` or a file under it
    cannot be read.
    """
    entries: list[Entry] = []

    def visit(directory: Path, prefix: str) -> None:
        with os.scandir(directory) as found:
            items = sorted(found, key=lambda item: item.name)
        for item in items:
            status = item.stat(follow_symlinks=False)
            entry: Entry = {
                "path": f"{prefix}{item.name}",
                "kind": _kind(status.st_mode),
                "mode": stat.S_IMODE(status.st_mode),
            }
            if entry["kind"] == "file":
                entry["sha256"] = hashlib.sha256(Path(item.path).read_bytes()).hexdigest()
            entries.append(entry)
            if entry["kind"] == "dir" and (prefix or item.name == SCREENS_DIRNAME):
                visit(Path(item.path), f"{entry['path']}/")

    visit(Path(root), "")
    return entries


# --- judge ----------------------------------------------------------------------------


def _bad_name(segment: str) -> bool:
    return any(unicodedata.category(char) in _NAME_CATEGORIES for char in segment)


def _name_code(segment: str) -> str | None:
    """The refusal code the name ``segment`` earns on its own, or ``None``."""
    if _bad_name(segment):
        return "bad_name"
    if segment.startswith(".") and segment.endswith(".tmp"):
        return "leftover_tmp"
    if segment.startswith("."):
        return "dotfile"
    return "ignored_name" if is_ignored_name(segment) else None


def _is_listed_name(name: str) -> bool:
    return name.endswith(SCREEN_SUFFIX) or os.path.splitext(name.lower())[1] in IMAGE_SUFFIXES


def _sort_key(text: str) -> bytes:
    return text.encode("utf-8", "surrogatepass")


def _case_collisions(paths: Iterable[str]) -> set[str]:
    """The spellings among ``paths`` and their directory prefixes that share a casefold with another."""
    names = set()
    for path in paths:
        parts = path.split("/")
        names.update("/".join(parts[:end]) for end in range(2, len(parts) + 1))
    groups: dict[str, set[str]] = {}
    for name in names:
        groups.setdefault(name.casefold(), set()).add(name)
    return {name for spellings in groups.values() if len(spellings) > 1 for name in spellings}


def judge(entries: Iterable[Entry]) -> Judged:
    """The list, the digest and the refusals for a root holding ``entries`` (see the module text)."""
    files: dict[str, str] = {}
    refused: set[tuple[str, str]] = set()
    below: list[str] = []
    for entry in entries:
        path, kind = entry["path"], entry["kind"]
        parts = path.split("/")
        if parts[-1] == SKIPPED_NAME and kind == "file":
            continue
        if parts[0] != SCREENS_DIRNAME or len(parts) == 1:
            if path == SCREENS_DIRNAME:
                if kind != "dir":
                    refused.add((path, "symlink" if kind == "symlink" else "outside_screens"))
            elif path in REPORT_NAMES and kind in ("file", "symlink"):
                if kind == "symlink":
                    refused.add((path, "symlink"))
            elif len(parts) == 1 and _name_code(path) == "leftover_tmp":
                refused.add((path, "leftover_tmp"))
            else:
                refused.add((parts[0], "outside_screens"))
            continue
        below.append(path)
        named = next(
            (("/".join(parts[: at + 1]), code) for at in range(1, len(parts)) if (code := _name_code(parts[at]))),
            None,
        )
        if named is not None:
            refused.add(named)
        elif kind == "symlink":
            refused.add((path, "symlink"))
        elif kind == "file":
            if _is_listed_name(parts[-1]):
                files[path] = entry.get("sha256", "")
            else:
                refused.add((path, "suffix"))
        elif kind != "dir":
            refused.add((path, "not_regular"))
    refused.update((name, "case_collision") for name in _case_collisions(below))
    prefix = len(SCREENS_DIRNAME) + 1
    return Judged(
        files=tuple(sorted(files.items(), key=lambda item: _sort_key(item[0][prefix:]))),
        refusals=tuple(
            {"path": path, "code": code} for path, code in sorted(refused, key=lambda r: (_sort_key(r[0]), r[1]))
        ),
    )
