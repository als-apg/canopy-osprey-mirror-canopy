"""The on-disk draft store: the only place canopy_mcp reads or writes draft screens.

Drafts live at ``<root>/screens/<id>.screen.json`` so the viewer can fetch them
with the same relative paths it uses for any screen tree. Every write is a
compare-and-replace against a *rev* (the sha256 of the file's bytes): the agent
and a human editing in the viewer can race, and a stale writer must be refused
rather than silently overwrite. This process is the only writer, so a single
``asyncio.Lock`` held by the store is enough; callers do their (slow) checking
before taking it and the store re-verifies the rev inside. Files are replaced
atomically (temp file in the target directory + ``os.replace``) so a reader
never sees half a document.
"""

import os
import re
import json
import asyncio
import hashlib
import tempfile
from typing import Any
from pathlib import Path
from urllib.parse import unquote

ID_PATTERN = re.compile(r"[A-Za-z0-9_-]+(/[A-Za-z0-9_-]+)*")
SCREEN_SUFFIX = ".screen.json"
IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".bmp", ".ico"})


class InvalidDraftId(ValueError):
    """The id does not match ``^[A-Za-z0-9_-]+(/[A-Za-z0-9_-]+)*$``."""


class DraftNotFound(LookupError):
    """No draft is stored under the id."""


class RevConflict(Exception):
    """One or more drafts are not at the rev the caller expected; nothing was written."""

    def __init__(self, conflicts: list[str]):
        self.conflicts = conflicts
        super().__init__(f"stale or conflicting rev for: {', '.join(conflicts)}")


def _serialize(doc: dict[str, Any]) -> bytes:
    return (json.dumps(doc, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _stat_key(st: os.stat_result) -> tuple[int, int, int]:
    return (st.st_ino, st.st_mtime_ns, st.st_size)


def doc_rev(doc: dict[str, Any]) -> str:
    """The rev ``doc`` will have once written: the sha256 of the bytes the store would write."""
    return _digest(_serialize(doc))


def contained_file(root: Path, relpath: Path | str) -> Path | None:
    """``root/relpath`` fully resolved, when that is a regular file inside ``root``; else ``None``.

    Symlinks are followed before the containment check, so one pointing outside
    ``root`` is refused.
    """
    try:
        base = root.resolve(strict=True)
        target = (root / relpath).resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    if not target.is_relative_to(base) or not target.is_file():
        return None
    return target


class DraftStore:
    def __init__(self, root: Path | str):
        self.root = Path(root)
        self.screens = self.root / "screens"
        self._lock = asyncio.Lock()
        # path -> ((st_ino, st_mtime_ns, st_size), rev); a changed stat means re-hash.
        self._revs: dict[Path, tuple[tuple[int, int, int], str]] = {}

    # --- ids and paths ------------------------------------------------------

    @staticmethod
    def validate_id(draft_id: str) -> str:
        if not isinstance(draft_id, str) or not ID_PATTERN.fullmatch(draft_id):
            raise InvalidDraftId(f"invalid draft id: {draft_id!r}")
        return draft_id

    def path_for(self, draft_id: str) -> Path:
        self.validate_id(draft_id)
        return self.screens / f"{draft_id}{SCREEN_SUFFIX}"

    # --- reading ------------------------------------------------------------

    def _rev_of(self, path: Path) -> str | None:
        try:
            st = path.stat()
        except FileNotFoundError:
            self._revs.pop(path, None)
            return None
        key = _stat_key(st)
        cached = self._revs.get(path)
        if cached and cached[0] == key:
            return cached[1]
        rev = _digest(path.read_bytes())
        self._revs[path] = (key, rev)
        return rev

    def rev(self, draft_id: str) -> str | None:
        """The current rev, or ``None`` when no such draft exists."""
        return self._rev_of(self.path_for(draft_id))

    def read(self, draft_id: str) -> tuple[dict[str, Any], str]:
        path = self.path_for(draft_id)
        try:
            data = path.read_bytes()
        except FileNotFoundError:
            raise DraftNotFound(draft_id) from None
        # Hash the bytes we parsed, so doc and rev always describe the same file state.
        rev = _digest(data)
        st = path.stat()
        if st.st_size == len(data):
            self._revs[path] = (_stat_key(st), rev)
        return json.loads(data), rev

    # --- writing ------------------------------------------------------------

    def _write_file(self, path: Path, data: bytes) -> str:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
        rev = _digest(data)
        self._revs[path] = (_stat_key(path.stat()), rev)
        return rev

    async def write(self, draft_id: str, doc: dict[str, Any], expect_rev: str | None) -> str:
        """Replace the draft if it is at ``expect_rev`` (``None``: it must not exist). Returns the new rev."""
        return (await self.write_many({draft_id: (doc, expect_rev)}))[draft_id]

    async def write_many(self, items: dict[str, tuple[dict[str, Any], str | None]]) -> dict[str, str]:
        """All-or-nothing: every rev is verified before any file is written.

        Raises ``RevConflict`` listing every conflicting id. Returns ``{id: new rev}``.
        """
        prepared = {
            draft_id: (self.path_for(draft_id), _serialize(doc), expect) for draft_id, (doc, expect) in items.items()
        }
        async with self._lock:
            conflicts = [draft_id for draft_id, (path, _, expect) in prepared.items() if self._rev_of(path) != expect]
            if conflicts:
                raise RevConflict(conflicts)
            return {draft_id: self._write_file(path, data) for draft_id, (path, data, _) in prepared.items()}

    async def delete(self, draft_id: str, expect_rev: str | None = None) -> None:
        """Remove the draft; when ``expect_rev`` is given it must match the current rev."""
        path = self.path_for(draft_id)
        async with self._lock:
            current = self._rev_of(path)
            if current is None:
                raise DraftNotFound(draft_id)
            if expect_rev is not None and current != expect_rev:
                raise RevConflict([draft_id])
            path.unlink()
            self._revs.pop(path, None)
            # Drop directories the draft leaves empty, never the screens root itself.
            parent = path.parent
            while parent != self.screens and parent.is_relative_to(self.screens):
                try:
                    parent.rmdir()
                except OSError:
                    break
                parent = parent.parent

    # --- listing ------------------------------------------------------------

    def manifest(self) -> dict[str, list[dict[str, Any]]]:
        """``{"screens": [...]}`` in the viewer's ``ManifestScreen`` shape, computed fresh on each call."""
        screens = []
        for draft_id in self.list():
            path = self.path_for(draft_id)
            try:
                doc, rev = self.read(draft_id)
            except (DraftNotFound, OSError):
                continue  # deleted between listing and reading
            except ValueError:
                doc, rev = None, self._rev_of(path)
            title = doc.get("metadata", {}).get("title") if isinstance(doc, dict) else None
            if not isinstance(title, str) or not title:
                title = draft_id
            screens.append(
                {
                    "id": draft_id,
                    "kind": "ir",
                    "category": "drafts",
                    "title": title,
                    "path": path.relative_to(self.root).as_posix(),
                    "rev": rev,
                }
            )
        return {"screens": screens}

    def resolve_static(self, relpath: str) -> Path | None:
        """The file under ``root`` a viewer request names, or ``None`` when it is missing or refused.

        Only draft screens and images are served. Anything that could leave the
        root -- ``..`` in any encoding, absolute paths, backslashes, NUL, symlinks
        pointing outside -- is refused.
        """
        decoded = relpath
        for _ in range(5):  # undo nested percent-encoding (%252e -> %2e -> .)
            nxt = unquote(decoded)
            if nxt == decoded:
                break
            decoded = nxt
        else:
            return None
        if not decoded or "\\" in decoded or "\x00" in decoded or decoded.startswith("/"):
            return None
        parts = decoded.split("/")
        if any(part in ("", ".", "..") for part in parts):
            return None
        name = parts[-1].lower()
        if not (name.endswith(SCREEN_SUFFIX) or Path(name).suffix in IMAGE_SUFFIXES):
            return None
        return contained_file(self.root, Path(*parts))

    def list(self) -> list[str]:
        """Every stored draft id, sorted."""
        if not self.screens.is_dir():
            return []
        ids = []
        for path in self.screens.rglob(f"*{SCREEN_SUFFIX}"):
            rel = path.relative_to(self.screens).as_posix()[: -len(SCREEN_SUFFIX)]
            if ID_PATTERN.fullmatch(rel) and path.is_file():
                ids.append(rel)
        return sorted(ids)
