"""The ``request_push`` tool: file a screen, with everything it needs, for the shared library.

A push does not change the library. It writes a *bundle* into the directory
``CANOPY_PUSH_DIR`` names: one directory per request holding a copy of the
screen, of every screen it embeds or opens and of every stored image they show,
and a ``request.json`` that describes them. An administrator reviews the bundle
on the host and accepts or rejects it there. Without ``CANOPY_PUSH_DIR`` the
tool is still offered and answers ``push_unavailable``.

The bundle is cut from one consistent state of the store, and nothing slow
happens under a lock:

1. **Read.** One synchronous section in a worker thread, under the store's
   ``flock``, runs :func:`~canopy_mcp.screens.push_closure` on a recording
   tree. It keeps every byte string the closure read and the rev of the top
   screen, and releases the lock. The tree stops at the first file that goes
   over a size or count cap, so neither the memory held nor the time under the
   lock grows with what a draft refers to.
2. **Judge.** With no lock held, the closure runs again on a tree of exactly
   those bytes, the grammar below is applied to them, and every screen among
   them is checked (:func:`~canopy_mcp.tools.check.run_check`, which may wait
   on the PV directory). A draft saved meanwhile is neither blocked nor read.
3. **Write.** A second synchronous section, under an ``flock`` on the push
   directory, writes those same bytes as the bundle and prunes old bundles.

**Grammar.** Each member is named by its path below ``source_dir``, which is
where it lands below ``screens/<target_dir>/``:

* a screen's id matches ``ID_PATTERN`` and an image's path segments match
  ``ASSET_SEGMENT``, and no segment is a reserved one
  (:func:`~canopy_mcp.store.is_reserved_segment`);
* an image carries one of ``PUSH_IMAGE_SUFFIXES`` (no SVG);
* at most ``MAX_ASSET_BYTES`` per file, :data:`MAX_FILES` files and
  :data:`MAX_TOTAL_BYTES` in total;
* no string anywhere in a pushed screen holds a forbidden character
  (:func:`~canopy_mcp.store.first_forbidden_pointer`);
* the paths and every directory prefix of them are unique under
  ``str.casefold()``.

**Bundle.** ``<push dir>/<rid>/files/<path below source_dir>`` for each member,
then ``<push dir>/<rid>/request.json``, written last through a temp file and a
rename, so a directory without it is an interrupted write and the next push
deletes it::

    {"format": "canopy-push/1", "rid": ..., "source_id": ..., "source_dir": ...,
     "rev": ..., "target_dir": ..., "note": ..., "created": ...,
     "files": [{"path": ..., "size": ..., "sha256": ...}, ...]}

``rid`` is 16 lower-case hex digits, ``rev`` the sha256 of the top screen's
bytes, ``created`` a UTC time (``2026-01-31T12:00:00Z``) and ``files`` is sorted
by path. Only the newest :data:`KEEP_BUNDLES` bundles are kept; the rids pruned
to make room are returned as ``evicted``.

Refusals are returned, not raised, as ``{draftId, refused: true, reason,
message, ...}``; nothing was written. ``reason`` is one of:

* ``read_only_store`` -- the id names a library screen;
* ``library_source`` -- the id's first segment is ``library``;
* ``push_unavailable`` -- no push directory is configured, or it cannot be written;
* ``closure_refused`` -- with ``refusals``, the :class:`~canopy_mcp.screens.Refusal`
  list of :func:`~canopy_mcp.screens.push_closure`;
* ``grammar_refused`` -- with ``refusals``, a list of ``{code, path, message}``
  (and ``pointer`` for ``invalid_text``, ``paths`` for ``case_collision``),
  ``code`` one of :data:`GRAMMAR_CODES`;
* ``check_errors`` -- with ``errors``, the error findings, each with the
  ``screen`` it was found in.

Bad arguments (an invalid id, ``source_dir``, ``target_dir`` or ``note``, an id
with no stored draft) raise :class:`ToolError`.
"""

from __future__ import annotations

import os
import json
import errno
import shutil
import asyncio
import hashlib
import secrets
from typing import Any
from pathlib import Path
from datetime import UTC, datetime
from collections.abc import Mapping

from fastmcp.exceptions import ToolError

from canopy_mcp.store import (
    LIBRARY,
    DIR_MODE,
    ID_PATTERN,
    ASSET_SEGMENT,
    SCREEN_SUFFIX,
    MAX_ASSET_BYTES,
    PUSH_IMAGE_SUFFIXES,
    DraftStore,
    ReadOnlyStore,
    lock_root,
    make_dirs,
    write_atomic,
    has_forbidden_char,
    casefold_collisions,
    is_reserved_segment,
    first_forbidden_pointer,
)
from canopy_mcp.config import Settings, get_settings
from canopy_mcp.server import mcp
from canopy_mcp.screens import PushRefused, push_closure
from canopy_mcp.check.refs import (
    StoreTree,
    BufferTree,
    dirname,
    dir_path,
    screen_key,
    screen_path,
)
from canopy_mcp.tools.check import run_check
from canopy_mcp.tools._common import route, refused, read_only_refusal

FORMAT = "canopy-push/1"
REQUEST_NAME = "request.json"
FILES_DIRNAME = "files"
MAX_FILES = 256
MAX_TOTAL_BYTES = 32 * 2**20
# What the host's listing reads of one ``request.json`` at most.
MAX_REQUEST_BYTES = 64 * 2**10
MAX_NOTE_CHARS = 2000
KEEP_BUNDLES = 8
GRAMMAR_CODES = (
    "bad_segment",
    "reserved_segment",
    "suffix",
    "file_too_large",
    "too_many_files",
    "bundle_too_large",
    "invalid_text",
    "case_collision",
    "request_too_large",
)
_RID_ATTEMPTS = 8
# What an exclusive ``mkdir`` answers when the push directory may not be written.
_UNWRITABLE = frozenset({errno.EROFS, errno.EACCES, errno.EPERM})


class _NoDraft(Exception):
    """The store holds no screen under the id asked for."""


# --- arguments ------------------------------------------------------------------------


def _source_dir(draft_id: str, source_dir: str | None) -> str:
    """The directory the push is cut from; :class:`ToolError` when it is not one that holds ``draft_id``."""
    if source_dir is None:
        return dirname(draft_id)
    if not isinstance(source_dir, str):
        raise ToolError("source_dir must be a string")
    if source_dir == "":
        if "/" in draft_id:
            raise ToolError(
                f"source_dir may be '' only for a top-level id; {draft_id!r} is inside {dirname(draft_id)!r}"
            )
        return ""
    if not ID_PATTERN.fullmatch(source_dir):
        raise ToolError(f"invalid source_dir: {source_dir!r}")
    if not draft_id.startswith(f"{source_dir}/"):
        raise ToolError(f"source_dir {source_dir!r} does not contain {draft_id!r}")
    return source_dir


def _check_target_dir(target_dir: str) -> None:
    if not isinstance(target_dir, str) or not ID_PATTERN.fullmatch(target_dir):
        raise ToolError(f"invalid target_dir: {target_dir!r} (a directory such as 'sr/rf', in draft-id form)")
    segments = target_dir.split("/")
    if segments[0] == LIBRARY:
        raise ToolError(f"target_dir {target_dir!r} may not start with the segment {LIBRARY!r}")
    for segment in segments:
        if is_reserved_segment(segment):
            raise ToolError(f"target_dir {target_dir!r} holds the reserved segment {segment!r}")


def _check_note(note: str) -> None:
    if not isinstance(note, str):
        raise ToolError("note must be a string")
    if len(note) > MAX_NOTE_CHARS:
        raise ToolError(f"note is {len(note)} characters; at most {MAX_NOTE_CHARS} are taken")
    if has_forbidden_char(note):
        raise ToolError("note holds a control, format or bidi character; write it as plain text")


# --- section 1: read ------------------------------------------------------------------


class _OverCap(Exception):
    """The closure went over a cap while it was being read: ``code`` and the store path that did it."""

    def __init__(self, code: str, path: str, message: str):
        self.code = code
        self.path = path
        super().__init__(message)


class _CappedTree(StoreTree):
    """A recording store tree that reads no more than a push may carry.

    Each path is read once (a second read answers the recorded bytes, so the
    closure sees one state of every file) and at most ``MAX_ASSET_BYTES + 1``
    bytes of it. The read that goes over the per-file, count or total cap
    raises :class:`_OverCap` instead of returning.
    """

    def __init__(self, store: DraftStore):
        super().__init__(store, record=True)
        self.total = 0

    def read(self, path: str) -> bytes:
        recorded = self.recorded
        assert recorded is not None
        if path in recorded:
            return recorded[path]
        file = self._file(path)
        if file is None:
            raise FileNotFoundError(path)
        if len(recorded) >= MAX_FILES:
            raise _OverCap("too_many_files", path, f"the push holds more than {MAX_FILES} files")
        with file.open("rb") as handle:
            data = handle.read(MAX_ASSET_BYTES + 1)
        if len(data) > MAX_ASSET_BYTES:
            raise _OverCap(
                "file_too_large", path, f"{path!r} is over {MAX_ASSET_BYTES} bytes, the most one file may be"
            )
        self.total += len(data)
        if self.total > MAX_TOTAL_BYTES:
            raise _OverCap("bundle_too_large", path, f"the push holds more than {MAX_TOTAL_BYTES} bytes")
        recorded[path] = data
        return data


def _snapshot(
    store: DraftStore, draft_id: str, source_dir: str
) -> tuple[dict[str, bytes], str] | PushRefused | _OverCap:
    """Every byte string the closure of ``draft_id`` reads, and the top rev, taken under the store's ``flock``.

    Synchronous and blocking: run it in a worker thread. The lock is shared, so
    two pushes read side by side while every writer waits.
    """
    top = screen_path(draft_id)
    try:
        with lock_root(store.root, exclusive=False):
            tree = _CappedTree(store)
            if not tree.has(top):
                raise _NoDraft(draft_id)
            try:
                push_closure(tree, draft_id, source_dir)
            except (PushRefused, _OverCap) as exc:
                return exc
            recorded = dict(tree.recorded or {})
    except (FileNotFoundError, NotADirectoryError):
        raise _NoDraft(draft_id) from None
    return recorded, hashlib.sha256(recorded[top]).hexdigest()


# --- section 2: judge -----------------------------------------------------------------


def _relative(path: str, source_dir: str) -> str:
    """The member ``path`` (a store path) as a path below ``source_dir``."""
    return path[len(dir_path(source_dir)) :]


def _invalid_text(data: bytes) -> tuple[str, str] | None:
    """``(JSON pointer, what is wrong)`` when the screen bytes ``data`` hold text a push refuses, else ``None``.

    The bytes are what is shipped, so they are judged as well as the document
    they parse to: they must be UTF-8, and a forbidden character that no parsed
    string holds (a byte-order mark, the value of a key written twice) is
    refused with the root pointer.
    """
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return "", "is not UTF-8"
    try:
        pointer = first_forbidden_pointer(json.loads(text))
    except ValueError:
        pointer = None  # not JSON: the closure refuses it, unless the cause is the character itself
    if pointer is not None:
        return pointer, f"holds a control, format or bidi character at {pointer!r}"
    if has_forbidden_char(text):
        return "", "holds a control, format or bidi character outside its strings (a byte-order mark, a repeated key)"
    return None


def _grammar(files: Mapping[str, bytes]) -> list[dict[str, Any]]:
    """Every way ``files`` (path below the source directory -> bytes) breaks the push grammar."""
    found: list[dict[str, Any]] = []

    def refuse(code: str, path: str, message: str, **extra: Any) -> None:
        found.append({"code": code, "path": path, "message": message, **extra})

    for path, data in sorted(files.items()):
        is_screen = path.endswith(SCREEN_SUFFIX)
        named = path[: -len(SCREEN_SUFFIX)] if is_screen else path
        segments = named.split("/")
        pattern = ID_PATTERN if is_screen else ASSET_SEGMENT
        if not all(pattern.fullmatch(segment) for segment in segments):
            refuse(
                "bad_segment", path, f"{path!r} is not a name a pushed {'screen' if is_screen else 'image'} may have"
            )
        for segment in segments:
            if is_reserved_segment(segment):
                refuse("reserved_segment", path, f"{path!r} holds the reserved name {segment!r}")
        if not is_screen and Path(path.lower()).suffix not in PUSH_IMAGE_SUFFIXES:
            refuse(
                "suffix", path, f"{path!r} is not an image a push may carry ({', '.join(sorted(PUSH_IMAGE_SUFFIXES))})"
            )
        if len(data) > MAX_ASSET_BYTES:
            refuse("file_too_large", path, f"{path!r} is {len(data)} bytes; at most {MAX_ASSET_BYTES} per file")
        if is_screen:
            invalid = _invalid_text(data)
            if invalid is not None:
                refuse("invalid_text", path, f"{path!r} {invalid[1]}", pointer=invalid[0])
    if len(files) > MAX_FILES:
        refuse("too_many_files", "", f"the push holds {len(files)} files; at most {MAX_FILES}")
    total = sum(len(data) for data in files.values())
    if total > MAX_TOTAL_BYTES:
        refuse("bundle_too_large", "", f"the push holds {total} bytes; at most {MAX_TOTAL_BYTES}")
    for spellings in casefold_collisions(files):
        message = f"{', '.join(repr(s) for s in spellings)} differ only in case, or name both a file and a directory"
        refuse("case_collision", spellings[0], message, paths=spellings)
    return found


def _request(
    rid: str,
    draft_id: str,
    source_dir: str,
    rev: str,
    target_dir: str,
    note: str,
    created: str,
    files: Mapping[str, bytes],
) -> bytes:
    """The bytes of ``request.json``."""
    doc = {
        "format": FORMAT,
        "rid": rid,
        "source_id": draft_id,
        "source_dir": source_dir,
        "rev": rev,
        "target_dir": target_dir,
        "note": note,
        "created": created,
        "files": [
            {"path": path, "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
            for path, data in sorted(files.items())
        ],
    }
    return (json.dumps(doc, indent=2, ensure_ascii=True) + "\n").encode("ascii")


def _now() -> str:
    """The current UTC time as ``request.json`` writes it (always 20 characters)."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


async def _check_errors(buffers: Mapping[str, bytes], settings: Settings) -> list[dict[str, Any]]:
    """The error findings of every screen among ``buffers`` (store path -> bytes), each with its ``screen``."""
    tree = BufferTree(buffers)
    errors: list[dict[str, Any]] = []
    for path in sorted(buffers):
        screen = screen_key(path)
        if screen is None:
            continue
        result = await run_check(draft_id=screen, tree=tree, settings=settings)
        errors.extend({"screen": screen, **finding.to_dict()} for finding in result.errors)
    return errors


# --- section 3: write -----------------------------------------------------------------


def _bundles(push_dir: Path) -> list[Path]:
    """The directories of ``push_dir`` (symlinks are not followed), in name order."""
    with os.scandir(push_dir) as entries:
        return sorted(Path(entry.path) for entry in entries if entry.is_dir(follow_symlinks=False))


def _write_bundle(
    push_dir: Path,
    draft_id: str,
    source_dir: str,
    rev: str,
    target_dir: str,
    note: str,
    files: Mapping[str, bytes],
) -> tuple[str, list[str]]:
    """Write ``files`` as a new bundle under ``push_dir``; returns its rid and the rids pruned.

    Synchronous and blocking: run it in a worker thread. Everything happens under
    an ``flock`` on ``push_dir``, which is created when missing. A bundle that
    cannot be completed is removed again.
    """
    make_dirs(push_dir)
    with lock_root(push_dir):
        for leftover in _bundles(push_dir):
            if not (leftover / REQUEST_NAME).is_file():
                shutil.rmtree(leftover, ignore_errors=True)
        for _ in range(_RID_ATTEMPTS):
            rid = secrets.token_hex(8)
            bundle = push_dir / rid
            try:
                os.mkdir(bundle, DIR_MODE)
            except FileExistsError:
                continue
            except OSError as exc:
                if exc.errno in _UNWRITABLE:
                    raise ReadOnlyStore(str(exc.strerror or exc)) from exc
                raise
            break
        else:
            raise OSError("no free bundle name")
        try:
            for path, data in sorted(files.items()):
                write_atomic(bundle / FILES_DIRNAME / path, data)
            request = _request(rid, draft_id, source_dir, rev, target_dir, note, _now(), files)
            write_atomic(bundle / REQUEST_NAME, request)
        except BaseException:
            shutil.rmtree(bundle, ignore_errors=True)
            raise

        def age(candidate: Path) -> tuple[int, str]:
            return ((candidate / REQUEST_NAME).stat().st_mtime_ns, candidate.name)

        # Newest first by the time ``request.json`` was written; the new bundle always stays.
        others = sorted((b for b in _bundles(push_dir) if b != bundle), key=age, reverse=True)
        evicted = others[KEEP_BUNDLES - 1 :]
        for old in evicted:
            shutil.rmtree(old, ignore_errors=True)
    return rid, sorted(old.name for old in evicted)


# --- the tool -------------------------------------------------------------------------


@mcp.tool
async def request_push(
    id: str,  # noqa: A002 - the name the contract gives it
    target_dir: str,
    note: str,
    source_dir: str | None = None,
) -> dict[str, Any]:
    """Ask for a draft to be added to the shared screen library. Nothing is published by this call.

    It files a request: a copy of screen ``id``, of every screen it embeds or
    opens (``open_display``) and of every stored image they show. An
    administrator reviews the request and accepts or rejects it. The files would
    land at ``screens/<target_dir>/<path below source_dir>`` of the library.

    * ``id`` -- your draft. A ``library/...`` screen cannot be pushed.
    * ``target_dir`` -- the library directory to land in, in draft-id form
      (``sr/rf``). It may not start with ``library`` or hold a reserved name.
    * ``note`` -- plain text for the reviewer: what the screen is and why it
      belongs in the library (at most 2000 characters).
    * ``source_dir`` -- the directory of your drafts that is copied; it must
      contain ``id`` and everything ``id`` refers to. Default: the directory of
      ``id``. ``""`` (the top level) is taken only for a top-level id.

    Returns ``{draftId, rid, rev, sourceDir, targetDir, files, evicted}``.
    ``rid`` names the request; give it to the user, since the reviewer needs it.
    ``files`` lists the copied paths below ``source_dir``. Only the newest 8
    requests are kept: ``evicted`` lists the rids of older requests this one
    displaced. Tell the user about every evicted rid, because a request that
    was not yet reviewed has to be pushed again.

    A refusal is ``{draftId, refused: true, reason, message, ...}`` and files
    nothing. ``reason``:

    * ``push_unavailable`` -- this server takes no pushes;
    * ``read_only_store`` / ``library_source`` -- ``id`` is a library screen;
    * ``closure_refused`` -- ``refusals`` lists refs or rules that would not
      mean the same in the library: an unresolved ref, a macro in a ref, a ref
      that leaves ``source_dir`` (push from a directory further up, or move the
      file), a root-relative or cross-root ref, a rule that retargets a ref or
      a PV channel;
    * ``grammar_refused`` -- ``refusals`` lists names and contents the library
      does not take: a reserved or malformed name, an SVG or non-image file, a
      file over 5 MiB, more than 256 files or 32 MiB, a control, format or bidi
      character in a screen's text (``pointer`` says where), names that differ
      only in case;
    * ``check_errors`` -- ``errors`` lists the error findings of ``check`` for
      each pushed ``screen``; fix them and push again.
    """
    settings = get_settings()
    store, local_id = route(id, settings)
    if refusal := read_only_refusal(id, store):
        return refusal
    if local_id.split("/", 1)[0] == LIBRARY:
        return refused(id, "library_source", f"{id!r} is under {LIBRARY!r}; a push comes from your own drafts")
    source = _source_dir(local_id, source_dir)
    _check_target_dir(target_dir)
    _check_note(note)
    if settings.push_dir is None:
        return refused(id, "push_unavailable", "push_unavailable: this server is not set up to take pushes")

    # 1. Read, under the store's lock.
    try:
        snapshot = await asyncio.to_thread(_snapshot, store, local_id, source)
    except _NoDraft:
        raise ToolError(f"no draft stored under {id!r}") from None
    if isinstance(snapshot, PushRefused):
        return _closure_refusal(id, snapshot)
    if isinstance(snapshot, _OverCap):
        over = {"code": snapshot.code, "path": _relative(snapshot.path, source), "message": str(snapshot)}
        return refused(id, "grammar_refused", f"refused: {snapshot}; nothing was filed", refusals=[over])
    buffers, rev = snapshot

    # 2. Judge those bytes, with no lock held.
    try:
        members = push_closure(BufferTree(buffers), local_id, source)
    except PushRefused as exc:
        return _closure_refusal(id, exc)
    files = {_relative(path, source): buffers[path] for path in members}
    broken = _grammar(files)
    # The trial has the length of the real one: a rid and a time are of fixed width.
    trial = _request("0" * 16, local_id, source, rev, target_dir, note, _now(), files)
    if not broken and len(trial) > MAX_REQUEST_BYTES:
        message = f"the request's file list is over {MAX_REQUEST_BYTES} bytes; push fewer files or shorter paths"
        broken = [{"code": "request_too_large", "path": "", "message": message}]
    if broken:
        message = f"refused: {len(broken)} name or content rule(s) are broken; nothing was filed"
        return refused(id, "grammar_refused", message, refusals=broken)
    errors = await _check_errors({path: buffers[path] for path in members}, settings)
    if errors:
        message = f"refused: the pushed screens have {len(errors)} error(s); nothing was filed"
        return refused(id, "check_errors", message, errors=errors)

    # 3. Write exactly those bytes, under the push directory's lock.
    try:
        rid, evicted = await asyncio.to_thread(
            _write_bundle, Path(settings.push_dir), local_id, source, rev, target_dir, note, files
        )
    except ReadOnlyStore:
        return refused(id, "push_unavailable", "push_unavailable: this server's push directory cannot be written")
    return {
        "draftId": id,
        "rid": rid,
        "rev": rev,
        "sourceDir": source,
        "targetDir": target_dir,
        "files": sorted(files),
        "evicted": evicted,
    }


def _closure_refusal(draft_id: str, exc: PushRefused) -> dict[str, Any]:
    message = f"refused: {len(exc.refusals)} ref(s) or rule(s) keep this screen from being pushed; nothing was filed"
    return refused(draft_id, "closure_refused", message, refusals=[dict(refusal) for refusal in exc.refusals])
