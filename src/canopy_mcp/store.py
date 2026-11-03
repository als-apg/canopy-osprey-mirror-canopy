"""The on-disk draft store: the only place canopy_mcp reads or writes draft screens.

Drafts live at ``<root>/screens/<id>.screen.json`` so the viewer can fetch them
with the same relative paths it uses for any screen tree. Every write is a
compare-and-replace against a *rev* (the sha256 of the file's bytes): the agent
and a human editing in the viewer can race, and a stale writer must be refused
rather than silently overwrite. Other processes write the same tree, so every
mutation runs as one synchronous section in a worker thread, under an exclusive
``flock`` on the root directory itself (``lock_root``; there is no lock file,
and the lock assumes a local filesystem). Callers do their (slow) checking
before the section and the store re-verifies the rev inside it. The store's
``asyncio.Lock`` stays in front, so one process queues its own writers without
parking threads. Files are replaced atomically (temp file in the target
directory + ``os.replace``) so a reader never sees half a document, and a
store that cannot be written answers ``ReadOnlyStore``.
"""

import os
import re
import json
import errno
import fcntl
import asyncio
import hashlib
import logging
import tempfile
from typing import Any, Literal
from pathlib import Path
from contextlib import contextmanager
from collections.abc import Iterable, Iterator

# The text rules live in canopy_mcp.text; re-exported for the callers that import them from here.
from canopy_mcp.text import (  # noqa: F401
    fully_unquoted,
    is_forbidden_char,
    has_forbidden_char,
    casefold_collisions,
)

ID_PATTERN = re.compile(r"[A-Za-z0-9_-]+(/[A-Za-z0-9_-]+)*")
SCREEN_SUFFIX = ".screen.json"
IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".bmp", ".ico"})
MAX_ASSET_BYTES = 5 * 2**20
# The manifest category of a personal store, and of the read-only library served beside one.
# A library screen is ``library/<id>`` to the tools and ``/embed/library/<id>`` to the viewer,
# so a personal store with a library beside it reserves ``library`` as a first id segment.
DRAFTS = "drafts"
LIBRARY = "library"
ASSET_SEGMENT = re.compile(r"[A-Za-z0-9 ._-]+")
# The suffixes a pushed image may carry: SVG is markup a browser executes, so it stays a draft.
PUSH_IMAGE_SUFFIXES = IMAGE_SUFFIXES - {".svg"}
# Path segments a pushed id or asset may not use: hidden names, names a command line would
# read as an option, and the leftovers of editors, interpreters and running processes. Three
# rule kinds, each compared on ``segment.casefold()`` (the tables hold folded forms): the exact
# names, a leading ``.`` or ``-``, and the endings. A deployment adds its own exact names
# (``CANOPY_LIBRARY_IGNORED_NAMES``, such as the files its build leaves out); read every rule
# through ``is_reserved_segment``.
RESERVED_SEGMENTS = frozenset({"__pycache__", ".git", ".venv", ".pytest_cache"})
RESERVED_PREFIXES = (".", "-")
RESERVED_ENDINGS = (".swp", ".swo", ".pyc", ".pyo", ".pyd", ".log", ".pid")
# Leading signatures of the raster formats a browser or Phoebus decodes; WEBP is the
# RIFF container with ``WEBP`` at offset 8 and is checked separately.
_IMAGE_MAGIC = (b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff", b"GIF87a", b"GIF89a", b"BM", b"\x00\x00\x01\x00")

# The process umask, read once at import: it can only be read by setting it, which races
# with any other thread creating files meanwhile. So this module must first be imported on
# the main thread, before worker threads start (the app imports it while it is built). New
# files get ``FILE_MODE`` and new directories ``DIR_MODE``.
UMASK = os.umask(0)
os.umask(UMASK)
FILE_MODE = 0o666 & ~UMASK
DIR_MODE = 0o777 & ~UMASK
# What ``mkdir``, ``mkstemp``, ``replace`` and ``unlink`` answer when the tree may not be written.
_READ_ONLY_ERRNOS = frozenset({errno.EROFS, errno.EACCES, errno.EPERM})

AssetStatus = Literal["written", "reused", "replaced"]

logger = logging.getLogger(__name__)

# ``(path, exception type)`` pairs ``DraftStore.manifest`` has already warned about. The
# viewer polls the manifest every second, so an unreadable draft warns once per process
# (across every store instance), not once per poll.
_MANIFEST_WARNED: set[tuple[Path, type[BaseException]]] = set()


def _warn_manifest_once(path: Path, exc: BaseException, consequence: str) -> None:
    key = (path, type(exc))
    if key in _MANIFEST_WARNED:
        return
    _MANIFEST_WARNED.add(key)
    logger.warning("draft manifest: %s: %s: %s (%s)", path, type(exc).__name__, exc, consequence)


# The longest description ``manifest(descriptions=True)`` returns; a library of a few
# hundred screens stays a cheap listing.
DESCRIPTION_MAX = 200

_SENTENCE_END = re.compile(r"(?<!\.\w\.)(?<=[.!?])\s(?=[A-Z])")


def summarize_description(text: object) -> str:
    """The first sentence of ``text`` on one line, cut to ``DESCRIPTION_MAX`` characters; ``""`` for a non-string."""
    if not isinstance(text, str):
        return ""
    line = " ".join(text.split())
    line = _SENTENCE_END.split(line, maxsplit=1)[0]
    if len(line) > DESCRIPTION_MAX:
        line = line[: DESCRIPTION_MAX - 1].rstrip() + "\u2026"
    return line


class InvalidDraftId(ValueError):
    """The id does not match ``^[A-Za-z0-9_-]+(/[A-Za-z0-9_-]+)*$``."""


class DraftNotFound(LookupError):
    """No draft is stored under the id."""


class AssetRefused(ValueError):
    """``write_asset`` refused the asset; ``reason`` is invalid_ref, too_large, not_image or conflict."""

    def __init__(self, reason: str, message: str):
        self.reason = reason
        super().__init__(message)


class RevConflict(Exception):
    """One or more drafts are not at the rev the caller expected; nothing was written."""

    def __init__(self, conflicts: list[str]):
        self.conflicts = conflicts
        super().__init__(f"stale or conflicting rev for: {', '.join(conflicts)}")


class ReadOnlyStore(Exception):
    """The store may not be written: it is read-only, or its tree refused the write.

    Deliberately not an ``OSError``, so a handler for I/O failures does not swallow
    it. The message starts with ``read_only_store``, the code callers answer with.
    """

    code = "read_only_store"

    def __init__(self, detail: str):
        super().__init__(f"{self.code}: {detail}")


@contextmanager
def _refusing(path: Path | str) -> Iterator[None]:
    """Turn EROFS, EACCES and EPERM raised inside the block into ``ReadOnlyStore``."""
    try:
        yield
    except OSError as exc:
        if exc.errno in _READ_ONLY_ERRNOS:
            raise ReadOnlyStore(f"{path}: {exc.strerror or exc}") from exc
        raise


@contextmanager
def lock_root(path: Path | str, *, exclusive: bool = True) -> Iterator[None]:
    """Hold an ``flock`` on the directory ``path`` for the length of the block.

    The lock is on the directory itself, through an ``O_RDONLY`` descriptor opened
    for this block and closed at its end: no lock file is created, and a read-only
    directory can be locked. ``exclusive=False`` takes a shared lock. The call
    blocks until the lock is granted, so use it in a worker thread, never on the
    event loop. ``flock`` locks belong to the open descriptor, so two blocks in one
    process exclude each other exactly as two processes do. ``path`` must exist
    (``FileNotFoundError`` otherwise; ``make_dirs`` creates it) and be a directory.
    Yields nothing.
    """
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
        yield
    finally:
        os.close(fd)  # closing the descriptor releases the lock


def make_dirs(path: Path | str) -> None:
    """Create the directory ``path`` and its missing parents with ``DIR_MODE``; fine when it exists."""
    with _refusing(path):
        Path(path).mkdir(mode=DIR_MODE, parents=True, exist_ok=True)


def write_atomic(path: Path, data: bytes) -> tuple[int, int, int]:
    """Replace ``path`` with ``data`` through a temp file beside it; returns the new file's stat key.

    The parent directory is created when missing. The file gets ``FILE_MODE``. The
    key comes from ``fstat`` of the descriptor that wrote the bytes, taken before
    the rename, which changes none of its three fields. Call it under ``lock_root``.
    """
    make_dirs(path.parent)
    with _refusing(path.parent):
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fchmod(fh.fileno(), FILE_MODE)
            os.fsync(fh.fileno())
            key = _stat_key(os.fstat(fh.fileno()))
        with _refusing(path):
            os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return key


def remove_file(path: Path) -> None:
    """Unlink ``path``; ``FileNotFoundError`` when it is absent. Call it under ``lock_root``."""
    with _refusing(path):
        os.unlink(path)


def _serialize(doc: dict[str, Any]) -> bytes:
    return (json.dumps(doc, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _stat_key(st: os.stat_result) -> tuple[int, int, int]:
    return (st.st_ino, st.st_mtime_ns, st.st_size)


def _read_keyed(path: Path) -> tuple[bytes, tuple[int, int, int] | None]:
    """The bytes of ``path`` and the stat key of the very file they came from.

    Both come from one descriptor, so a file swapped in at ``path`` meanwhile cannot
    lend its key to these bytes. The key is ``None`` when the file changed in place
    during the read; such bytes must not be cached.
    """
    with open(path, "rb") as fh:
        before = _stat_key(os.fstat(fh.fileno()))
        data = fh.read()
        after = _stat_key(os.fstat(fh.fileno()))
    return data, after if before == after else None


def doc_rev(doc: dict[str, Any]) -> str:
    """The rev ``doc`` will have once written: the sha256 of the bytes the store would write."""
    return _digest(_serialize(doc))


def _clean_parts(relpath: str) -> list[str] | None:
    """``relpath`` split on ``/`` when it is structurally safe, else ``None``.

    Refuses empty paths, backslashes, NUL, a leading ``/`` and any empty, ``.``
    or ``..`` segment. No percent-decoding happens here.
    """
    if not relpath or "\\" in relpath or "\x00" in relpath or relpath.startswith("/"):
        return None
    parts = relpath.split("/")
    if any(part in ("", ".", "..") for part in parts):
        return None
    return parts


def asset_relpath_reason(relpath: str) -> Literal["invalid_ref"] | None:
    """``'invalid_ref'`` when ``relpath`` (draft-id space) cannot name a stored image, else ``None``.

    Every segment must fullmatch ``ASSET_SEGMENT`` and none may end in
    ``.screen.json``; the last one must carry an ``IMAGE_SUFFIXES`` suffix.
    """
    parts = _clean_parts(relpath) if isinstance(relpath, str) else None
    if parts is None:
        return "invalid_ref"
    for part in parts:
        if not ASSET_SEGMENT.fullmatch(part) or part.lower().endswith(SCREEN_SUFFIX):
            return "invalid_ref"
    if Path(parts[-1].lower()).suffix not in IMAGE_SUFFIXES:
        return "invalid_ref"
    return None


def looks_like_image(data: bytes, suffix: str) -> bool:
    """Whether ``data`` plausibly is an image for ``suffix`` (lower-case, with the dot).

    ``.svg`` needs ``<`` as its first byte after an optional UTF-8 BOM and
    whitespace; every other image suffix accepts any known raster signature, since
    browsers decode by signature, not by name.
    """
    if suffix == ".svg":
        return data.removeprefix(b"\xef\xbb\xbf").lstrip().startswith(b"<")
    if suffix not in IMAGE_SUFFIXES:
        return False
    return data.startswith(_IMAGE_MAGIC) or (data[:4] == b"RIFF" and data[8:12] == b"WEBP")


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


def is_reserved_segment(segment: str, extra: Iterable[str] = ()) -> bool:
    """Whether ``segment`` is one the push grammar reserves.

    True for a ``RESERVED_SEGMENTS`` name or an ``extra`` name (the deployment's
    ``CANOPY_LIBRARY_IGNORED_NAMES``, case-folded), a ``RESERVED_PREFIXES`` start or a
    ``RESERVED_ENDINGS`` end, each compared on ``segment.casefold()``: ``__PYCACHE__``
    and ``x.SWP`` are reserved. A pure check; it does not say whether the segment
    matches ``ID_PATTERN`` or ``ASSET_SEGMENT``.
    """
    folded = segment.casefold()
    return (
        folded in RESERVED_SEGMENTS
        or folded in extra
        or folded.startswith(RESERVED_PREFIXES)
        or folded.endswith(RESERVED_ENDINGS)
    )


def first_forbidden_pointer(doc: Any) -> str | None:
    """The JSON pointer of the first string in ``doc`` that holds a forbidden character, else ``None``.

    Forbidden is the free-text rule of :mod:`canopy_mcp.text` (tab, LF and CR allowed).

    ``doc`` is a decoded JSON value. The walk is depth-first in document order: an
    object's members in their stored order, each key before its value, and a list by
    index. An offending value reports its own pointer; an offending key reports the
    pointer of the object that holds it, so a returned pointer never contains a
    forbidden character itself. Pointers follow RFC 6901 (``~`` is written ``~0`` and
    ``/`` is written ``~1``); the root is the empty string.
    """
    if isinstance(doc, str):
        return "" if has_forbidden_char(doc) else None
    # An explicit stack of (pointer, iterator) keeps the depth off the interpreter's.
    stack: list[tuple[str, Any]] = []
    if isinstance(doc, dict):
        stack.append(("", iter(doc.items())))
    elif isinstance(doc, list):
        stack.append(("", iter(enumerate(doc))))
    while stack:
        pointer, members = stack[-1]
        entry = next(members, None)
        if entry is None:
            stack.pop()
            continue
        key, value = entry
        if isinstance(key, str):
            if has_forbidden_char(key):
                return pointer
            token = key.replace("~", "~0").replace("/", "~1")
        else:
            token = str(key)
        child = f"{pointer}/{token}"
        if isinstance(value, str):
            if has_forbidden_char(value):
                return child
        elif isinstance(value, dict):
            stack.append((child, iter(value.items())))
        elif isinstance(value, list):
            stack.append((child, iter(enumerate(value))))
    return None


class DraftStore:
    def __init__(
        self,
        root: Path | str,
        *,
        read_only: bool = False,
        category: str = DRAFTS,
        reserved: Iterable[str] = (),
    ):
        self.root = Path(root)
        self.screens = self.root / "screens"
        # A read-only store refuses every mutation before it touches the disk; reads are unaffected.
        self.read_only = read_only
        # The manifest category. A store of any other category than ``drafts`` is served under
        # ``/drafts/<category>/``: its manifest paths carry that prefix and only ``screens/`` is served.
        self.category = category
        # First id segments this store neither lists, serves nor accepts (``check_id``).
        self.reserved = frozenset(reserved)
        self._lock = asyncio.Lock()
        # path -> ((st_ino, st_mtime_ns, st_size), rev); a changed stat means re-hash.
        self._revs: dict[Path, tuple[tuple[int, int, int], str]] = {}

    # --- ids and paths ------------------------------------------------------

    @staticmethod
    def validate_id(draft_id: str) -> str:
        if not isinstance(draft_id, str) or not ID_PATTERN.fullmatch(draft_id):
            raise InvalidDraftId(f"invalid draft id: {draft_id!r}")
        return draft_id

    def reserves(self, draft_id: str) -> bool:
        """Whether the first segment of ``draft_id`` is one this store reserves."""
        return draft_id.split("/", 1)[0] in self.reserved

    def check_id(self, draft_id: str) -> str:
        """``draft_id`` when it is valid *in this store*, else :class:`InvalidDraftId`.

        Stricter than :meth:`validate_id` by the reserved first segments, which only
        an instance knows. :meth:`path_for` stays syntactic, so resolving a ref that
        lands on a reserved id answers "no such file" instead of raising.
        """
        self.validate_id(draft_id)
        if self.reserves(draft_id):
            first = draft_id.split("/", 1)[0]
            raise InvalidDraftId(f"invalid draft id: {draft_id!r} ({first!r} is reserved as a first segment)")
        return draft_id

    def path_for(self, draft_id: str) -> Path:
        self.validate_id(draft_id)
        return self.screens / f"{draft_id}{SCREEN_SUFFIX}"

    # --- reading ------------------------------------------------------------

    def _contained(self, path: Path) -> Path | None:
        """``path`` fully resolved when it is a regular file inside the root, else ``None``.

        Every draft read goes through here, so a symlink under ``screens/`` that
        points outside the store is never read, hashed, listed or served; it
        counts as no draft at all.
        """
        return contained_file(self.root, path.relative_to(self.root))

    def _remember(self, path: Path, key: tuple[int, int, int] | None, rev: str) -> None:
        if key is None:
            self._revs.pop(path, None)
        else:
            self._revs[path] = (key, rev)

    def _rev_of(self, path: Path) -> str | None:
        target = self._contained(path)
        try:
            if target is None:
                raise FileNotFoundError(path)
            st = target.stat()
        except FileNotFoundError:
            self._revs.pop(path, None)
            return None
        cached = self._revs.get(path)
        # The cheap path: an unchanged stat answers without opening the file.
        if cached and cached[0] == _stat_key(st):
            return cached[1]
        try:
            data, key = _read_keyed(target)
        except FileNotFoundError:
            self._revs.pop(path, None)
            return None
        rev = _digest(data)
        self._remember(path, key, rev)
        return rev

    def _at_rev(self, path: Path, expect: str | None) -> bool:
        """Whether the draft at ``path`` is at ``expect`` (``None``: nothing is there).

        An entry that exists but resolves outside the store reads as no draft, yet
        it is not absent: it never matches ``None``, so a create cannot write
        through it.
        """
        if expect is None and os.path.lexists(path):
            return False
        return self._rev_of(path) == expect

    def rev(self, draft_id: str) -> str | None:
        """The current rev, or ``None`` when no such draft exists."""
        return self._rev_of(self.path_for(draft_id))

    def read(self, draft_id: str) -> tuple[dict[str, Any], str]:
        path = self.path_for(draft_id)
        target = self._contained(path)
        try:
            if target is None:
                raise FileNotFoundError(path)
            data, key = _read_keyed(target)
        except FileNotFoundError:
            raise DraftNotFound(draft_id) from None
        # Hash the bytes we parsed, so doc and rev always describe the same file state.
        rev = _digest(data)
        self._remember(path, key, rev)
        return json.loads(data), rev

    # --- writing ------------------------------------------------------------

    def _refuse_read_only(self) -> None:
        if self.read_only:
            raise ReadOnlyStore(f"{self.root} is a read-only store")

    def _write_file(self, path: Path, data: bytes) -> str:
        rev = _digest(data)
        self._revs[path] = (write_atomic(path, data), rev)
        return rev

    async def write(self, draft_id: str, doc: dict[str, Any], expect_rev: str | None) -> str:
        """Replace the draft if it is at ``expect_rev`` (``None``: it must not exist). Returns the new rev."""
        return (await self.write_many({draft_id: (doc, expect_rev)}))[draft_id]

    async def write_many(self, items: dict[str, tuple[dict[str, Any], str | None]]) -> dict[str, str]:
        """All-or-nothing: every rev is verified before any file is written.

        Raises ``RevConflict`` listing every conflicting id. Returns ``{id: new rev}``.
        """
        self._refuse_read_only()
        prepared = {
            draft_id: (self.path_for(draft_id), _serialize(doc), expect) for draft_id, (doc, expect) in items.items()
        }
        async with self._lock:
            return await asyncio.to_thread(self._write_many_locked, prepared)

    def _write_many_locked(self, prepared: dict[str, tuple[Path, bytes, str | None]]) -> dict[str, str]:
        make_dirs(self.root)
        with lock_root(self.root):
            # As for assets: a symlinked store root is fine, a screens/ (or any
            # directory under it) resolving elsewhere is not. Checked for every
            # draft before any directory is made or any file written.
            base = self.root.resolve(strict=True) / "screens"
            for draft_id, (path, _, _) in prepared.items():
                parent = path.parent.resolve(strict=False)
                if not parent.is_relative_to(base):
                    raise ReadOnlyStore(f"{draft_id}: its directory resolves outside the store's screens/")
            conflicts = [draft_id for draft_id, (path, _, expect) in prepared.items() if not self._at_rev(path, expect)]
            if conflicts:
                raise RevConflict(conflicts)
            return {draft_id: self._write_file(path, data) for draft_id, (path, data, _) in prepared.items()}

    async def write_asset(self, relpath: str, data: bytes, *, overwrite: bool = False) -> AssetStatus:
        """Store image ``data`` at ``screens/<relpath>``; returns written, reused or replaced.

        Raises ``AssetRefused`` with ``reason`` checked in this order: invalid_ref
        (``asset_relpath_reason``), too_large (over ``MAX_ASSET_BYTES``), not_image
        (``looks_like_image``), conflict (something on disk is in the way: a
        ``screens/`` or intermediate directory resolving outside the store, a
        symlink or non-file at the target, or a differing file without
        ``overwrite``). ``OSError`` propagates -- a dangling ``screens`` link
        surfaces as the ``FileExistsError`` pathlib raises.
        """
        self._refuse_read_only()
        if asset_relpath_reason(relpath) is not None:
            raise AssetRefused("invalid_ref", f"not a valid asset path: {relpath!r}")
        if len(data) > MAX_ASSET_BYTES:
            raise AssetRefused("too_large", f"{len(data)} bytes exceeds the {MAX_ASSET_BYTES}-byte asset limit")
        suffix = Path(relpath.split("/")[-1].lower()).suffix
        if not looks_like_image(data, suffix):
            detail = (
                "bytes are not an SVG document"
                if suffix == ".svg"
                else "bytes carry no PNG/JPEG/GIF/BMP/ICO/WEBP signature"
            )
            raise AssetRefused("not_image", detail)
        async with self._lock:
            return await asyncio.to_thread(self._write_asset_locked, relpath, data, overwrite)

    def _write_asset_locked(self, relpath: str, data: bytes, overwrite: bool) -> AssetStatus:
        target = self.screens / relpath
        make_dirs(self.root)
        with lock_root(self.root):
            make_dirs(self.screens)
            # One level below contained_file's base: a symlinked store root is fine,
            # a screens/ (or any directory under it) resolving elsewhere is not.
            base = self.root.resolve(strict=True) / "screens"
            parent = target.parent.resolve(strict=False)
            if not parent.is_relative_to(base):
                raise AssetRefused(
                    "conflict", f"{relpath}: its directory resolves to {parent}, outside the store's {base}"
                )
            if target.is_symlink():
                raise AssetRefused("conflict", f"{relpath}: a symlink is in the way at screens/{relpath}")
            if target.exists():
                if not target.is_file():
                    what = "a directory" if target.is_dir() else "a non-regular file"
                    raise AssetRefused("conflict", f"{relpath}: {what} is in the way at screens/{relpath}")
                same = target.stat().st_size == len(data) and _digest(target.read_bytes()) == _digest(data)
                if same:
                    return "reused"
                if not overwrite:
                    raise AssetRefused(
                        "conflict",
                        f"{relpath}: a different file already exists at screens/{relpath}; overwrite=true replaces it",
                    )
                self._write_file(target, data)
                return "replaced"
            self._write_file(target, data)
            return "written"

    async def delete(self, draft_id: str, expect_rev: str | None = None) -> None:
        """Remove the draft; when ``expect_rev`` is given it must match the current rev."""
        self._refuse_read_only()
        path = self.path_for(draft_id)
        async with self._lock:
            await asyncio.to_thread(self._delete_locked, draft_id, path, expect_rev)

    def _delete_locked(self, draft_id: str, path: Path, expect_rev: str | None) -> None:
        try:
            # A store whose root was never created holds no draft; deleting does not create it.
            with lock_root(self.root):
                current = self._rev_of(path)
                if current is None:
                    raise DraftNotFound(draft_id)
                if expect_rev is not None and current != expect_rev:
                    raise RevConflict([draft_id])
                remove_file(path)
                self._revs.pop(path, None)
                # Drop directories the draft leaves empty, never the screens root itself.
                parent = path.parent
                while parent != self.screens and parent.is_relative_to(self.screens):
                    try:
                        parent.rmdir()
                    except OSError:
                        break
                    parent = parent.parent
        except FileNotFoundError:
            raise DraftNotFound(draft_id) from None

    # --- listing ------------------------------------------------------------

    def manifest(self, *, descriptions: bool = False) -> dict[str, list[dict[str, Any]]]:
        """``{"screens": [...]}`` in the viewer's ``ManifestScreen`` shape, computed fresh on each call.

        Entries carry the store's ``category``. ``path`` is what the viewer fetches under
        ``/drafts/``: ``screens/<id>.screen.json`` for a personal store, and
        ``<category>/screens/<id>.screen.json`` for any other. ``id`` is always the id
        this store reads the screen by.

        With ``descriptions``, each entry also carries ``description``: the first
        sentence of ``metadata.description``, at most ``DESCRIPTION_MAX`` characters,
        ``""`` when the screen has none. The viewer's manifest leaves it out.
        """
        prefix = "" if self.category == DRAFTS else f"{self.category}/"
        screens = []
        for draft_id in self.list():
            path = self.path_for(draft_id)
            try:
                doc, rev = self.read(draft_id)
            except (DraftNotFound, FileNotFoundError):
                continue  # deleted between listing and reading
            except OSError as exc:
                _warn_manifest_once(path, exc, "left out of the manifest")
                continue
            except ValueError as exc:
                _warn_manifest_once(path, exc, "listed with its id as the title")
                doc, rev = None, self._rev_of(path)
            metadata = doc.get("metadata") if isinstance(doc, dict) else None
            if not isinstance(metadata, dict):
                metadata = {}
            title = metadata.get("title")
            if not isinstance(title, str) or not title:
                title = draft_id
            entry = {
                "id": draft_id,
                "kind": "ir",
                "category": self.category,
                "title": title,
                "path": prefix + path.relative_to(self.root).as_posix(),
                "rev": rev,
            }
            if descriptions:
                entry["description"] = summarize_description(metadata.get("description"))
            screens.append(entry)
        return {"screens": screens}

    def resolve_static(self, relpath: str) -> Path | None:
        """The file under ``root`` a viewer request names, or ``None`` when it is missing or refused.

        Only draft screens and images are served. Anything that could leave the
        root -- ``..`` in any encoding, absolute paths, backslashes, NUL, symlinks
        pointing outside -- is refused. A store of another category than ``drafts``
        serves only what lies under ``screens/``, and a reserved first segment (at
        the root or under ``screens/``) is never served. Both rules apply to the
        decoded path.
        """
        decoded = fully_unquoted(relpath)  # undo nested percent-encoding (%252e -> %2e -> .)
        if decoded is None:
            return None
        parts = _clean_parts(decoded)
        if parts is None:
            return None
        if self.category != DRAFTS and parts[0] != "screens":
            return None
        if parts[0] in self.reserved or (parts[0] == "screens" and len(parts) > 1 and parts[1] in self.reserved):
            return None
        name = parts[-1].lower()
        if not (name.endswith(SCREEN_SUFFIX) or Path(name).suffix in IMAGE_SUFFIXES):
            return None
        return contained_file(self.root, Path(*parts))

    def list(self) -> list[str]:
        """Every stored draft id, sorted.

        Ids under a reserved first segment are left out, and so is any screen file
        that resolves outside the store (a symlink pointing elsewhere).
        """
        if not self.screens.is_dir():
            return []
        ids = []
        for path in self.screens.rglob(f"*{SCREEN_SUFFIX}"):
            rel = path.relative_to(self.screens).as_posix()[: -len(SCREEN_SUFFIX)]
            if ID_PATTERN.fullmatch(rel) and not self.reserves(rel) and self._contained(path) is not None:
                ids.append(rel)
        return sorted(ids)


def open_stores(drafts_dir: Path | str, library_dir: Path | str | None) -> tuple[DraftStore, DraftStore | None]:
    """The personal store over ``drafts_dir`` and the read-only library store over ``library_dir``.

    Without a library the personal store is exactly ``DraftStore(drafts_dir)`` and
    the second item is ``None``. With one, the personal store reserves the
    ``library`` first segment, so no personal id can shadow a library screen.
    """
    if library_dir is None:
        return DraftStore(drafts_dir), None
    return (
        DraftStore(drafts_dir, reserved=(LIBRARY,)),
        DraftStore(library_dir, read_only=True, category=LIBRARY),
    )
