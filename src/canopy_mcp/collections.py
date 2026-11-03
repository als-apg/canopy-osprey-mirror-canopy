"""Collections: named, ordered sets of things the viewer can open.

The format, the rules, the stores and the HTTP surface are specified in
``COLLECTIONS.md`` beside this module; this module is their one implementation.
:func:`validate_collection` checks a document against every rule (the personal
store's ``PUT``, both readers and ``canopy-collections-check`` call it).
:class:`FacilityCollections` reads the deployment's read-only directory,
:class:`CollectionStore` keeps the user's own collections and the per-user state
(ticked, pinned, recent) under compare-and-swap revs, and
:func:`build_collection_routes` serves ``/collections`` and ``/collections-state``.
The layout tree is :mod:`canopy_mcp.layout`, the member identity
:mod:`canopy_mcp.member_key`.
"""

from __future__ import annotations

import json
import asyncio
import hashlib
import logging
from typing import Any, Literal
from pathlib import Path
from datetime import UTC, datetime
from collections.abc import Mapping, Iterable

from starlette.routing import Route
from starlette.requests import Request
from starlette.responses import Response

from canopy_mcp.http import (
    NO_CACHE,
    ALL_METHODS,
    etag,
    not_modified,
    is_cross_site,
    json_response,
    error_response,
    is_json_request,
    read_capped_body,
)
from canopy_mcp.store import (
    DRAFTS,
    LIBRARY,
    ID_PATTERN,
    SCREEN_SUFFIX,
    ReadOnlyStore,
    lock_root,
    make_dirs,
    remove_file,
    write_atomic,
)
from canopy_mcp.layout import (
    SPLITS,
    LAYOUT_REASONS,
    LayoutForm,
    LayoutRefused,
    check_layout,
    normalize_layout,
)
from canopy_mcp.listing import Judged, scan, judge, ignored_names_from_env
from canopy_mcp.validate import (
    TOOL_IDS,
    MAX_QUERY_BYTES,
    VIEW_ID_PATTERN,
    InvalidValue,
    clean_name,
    check_query,
    compact_size,
)
from canopy_mcp.member_key import InvalidMember, member_key

__all__ = [
    "COLLECTIONS_PATH",
    "FACILITY",
    "PERSONAL",
    "SOURCES",
    "MAX_MEMBERS",
    "MAX_LAYOUT_BYTES",
    "MAX_COLLECTION_BYTES",
    "MAX_QUERY_BYTES",
    "SPLITS",
    "MAX_STATE_OP_BYTES",
    "MAX_STATE_BYTES",
    "MAX_PINNED",
    "MAX_REFS",
    "MAX_RECENT",
    "MAX_PUT_BODY_BYTES",
    "MAX_POST_BODY_BYTES",
    "COLLECTIONS_CACHE_CONTROL",
    "STATE_FILE",
    "STATE_OPS",
    "InvalidCollection",
    "InvalidLayout",
    "LAYOUT_REASONS",
    "layout_refusal",
    "normalize_layout",
    "InvalidCollectionId",
    "InvalidStateOp",
    "CollectionNotFound",
    "CollectionExists",
    "StateFull",
    "PinnedFull",
    "SelectionFull",
    "StateTooLarge",
    "ReadOnlyStore",
    "FacilityCollections",
    "CollectionStore",
    "all_collections",
    "build_collection_routes",
    "empty_state",
    "library_screens",
    "shown_refs",
    "validate_collection",
    "validate_collection_id",
    "validate_ref",
    "validate_state_member",
]

# The path config.json advertises for these routes; the state lives at ``<path>-state``.
COLLECTIONS_PATH = "collections"
FACILITY = "facility"
PERSONAL = "personal"
SOURCES = (FACILITY, PERSONAL)
MAX_MEMBERS = 50
MAX_LAYOUT_BYTES = 32 * 1024
MAX_COLLECTION_BYTES = 256 * 1024
MAX_STATE_OP_BYTES = 8 * 1024
MAX_STATE_BYTES = 512 * 1024
MAX_PINNED = 50
MAX_REFS = 100
MAX_RECENT = 10
COLLECTION_SUFFIX = ".json"
# Transport caps on a request body, above the document limits so a body between the
# two answers the limit's own 400 (collection) or 413 (op) naming the size.
MAX_PUT_BODY_BYTES = 4 * MAX_COLLECTION_BYTES
MAX_POST_BODY_BYTES = 8 * MAX_STATE_OP_BYTES
COLLECTIONS_CACHE_CONTROL = NO_CACHE
STATE_FILE = ".state.json"
STATE_LISTS = ("ticked", "unticked", "pinned", "recent")
# Each state op and the one field it carries besides ``op`` (``None``: no field).
OP_FIELDS: dict[str, str | None] = {
    "tick": "ref",
    "untick": "ref",
    "pin": "member",
    "unpin": "member",
    "recent": "member",
    "recent-remove": "member",
    "recent-clear": None,
    "recent-many": "members",
}
STATE_OPS = tuple(OP_FIELDS)
# ``recent-many`` carries up to MAX_RECENT members, so it gets its own op cap.
MAX_RECENT_MANY_OP_BYTES = 48 * 1024

log = logging.getLogger(__name__)

_COLLECTION_KEYS = frozenset({"id", "name", "description", "members", "layout", "default", "updated"})
_SCREEN_KEYS = frozenset({"screen", "macros"})
_TOOL_KEYS = frozenset({"tool", "query", "name"})
_SCREENS_PREFIX = "screens/"


class InvalidCollection(ValueError):
    """The document breaks a collection rule; the message names the field and the rule."""


class InvalidLayout(InvalidCollection):
    """The layout breaks a layout rule: ``reason`` (one of :data:`LAYOUT_REASONS`) at ``where``."""

    def __init__(self, reason: str, where: str):
        super().__init__(f"{where}: {reason}: {_REASON_TEXT[reason]}")
        self.reason = reason
        self.where = where


def _text(value: Any, where: str) -> str:
    if not isinstance(value, str):
        raise InvalidCollection(f"{where} must be a string")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise InvalidCollection(f"{where} must not contain lone surrogates") from None
    return value


def _name(value: Any, where: str) -> str:
    try:
        return clean_name(value)
    except InvalidValue as exc:
        raise InvalidCollection(f"{where}: {exc}") from None


def _id_value(value: Any, where: str) -> str:
    if not isinstance(value, str) or not VIEW_ID_PATTERN.fullmatch(value):
        raise InvalidCollection(f"{where} must match ^[A-Za-z0-9_-]{{1,64}}$")
    return value


def _unknown(doc: Mapping[str, Any], allowed: frozenset[str], where: str) -> None:
    extra = sorted(str(key) for key in doc if key not in allowed)
    if extra:
        raise InvalidCollection(f"{where} has unknown key(s): {', '.join(extra)}")


def library_screens(library: Path | str | Judged) -> frozenset[str]:
    """The screen ids (``<id>`` of ``library/<id>``) a library root or its judgement lists.

    A root is judged with the deployment's ignored names (``CANOPY_LIBRARY_IGNORED_NAMES``),
    as the server's own listing is, so a screen the listing leaves out is no member.
    """
    if isinstance(library, Judged):
        judged = library
    else:
        judged = judge(scan(Path(library)), ignored_names=ignored_names_from_env())
    refused = {refusal["path"] for refusal in judged.refusals}
    ids = set()
    for path in judged.paths:
        if not (path.startswith(_SCREENS_PREFIX) and path.endswith(SCREEN_SUFFIX)):
            continue
        parts = path.split("/")
        if any("/".join(parts[: at + 1]) in refused for at in range(len(parts))):
            continue
        ids.add(path[len(_SCREENS_PREFIX) : -len(SCREEN_SUFFIX)])
    return frozenset(ids)


def _screen_member(member: Mapping[str, Any], where: str, source: str, listed: frozenset[str] | None) -> dict:
    _unknown(member, _SCREEN_KEYS, where)
    ref = member["screen"]
    if not isinstance(ref, str) or "/" not in ref:
        raise InvalidCollection(f"{where}.screen must be '<category>/<id>'")
    category, screen_id = ref.split("/", 1)
    if category not in (LIBRARY, DRAFTS):
        raise InvalidCollection(f"{where}.screen category must be {LIBRARY!r} or {DRAFTS!r}")
    if not ID_PATTERN.fullmatch(screen_id):
        raise InvalidCollection(f"{where}.screen id must match [A-Za-z0-9_-]+(/[A-Za-z0-9_-]+)*")
    if category == DRAFTS and source == FACILITY:
        raise InvalidCollection(f"{where}.screen: a facility collection names library/* screens only")
    if category == LIBRARY and listed is not None and screen_id not in listed:
        raise InvalidCollection(f"{where}.screen {ref!r} is not a screen the library lists")
    out: dict[str, Any] = {"screen": ref}
    if "macros" in member:
        macros = member["macros"]
        if not isinstance(macros, Mapping):
            raise InvalidCollection(f"{where}.macros must be an object")
        for key, value in macros.items():
            _text(key, f"{where}.macros name")
            _text(value, f"{where}.macros[{key!r}]")
        out["macros"] = dict(macros)
    return out


def _tool_member(member: Mapping[str, Any], where: str, source: str) -> dict:
    _unknown(member, _TOOL_KEYS, where)
    tool = member["tool"]
    if tool not in TOOL_IDS:
        raise InvalidCollection(f"{where}.tool must be one of {', '.join(TOOL_IDS)}")
    out: dict[str, Any] = {"tool": tool}
    if "query" in member:
        try:
            out["query"] = check_query(member["query"])
        except InvalidValue as exc:
            raise InvalidCollection(f"{where}.query: {exc} (MAX_QUERY_BYTES)") from None
    if "name" in member:
        out["name"] = _name(member["name"], f"{where}.name")
    elif source == FACILITY:
        raise InvalidCollection(f"{where}: every tool member of a facility collection carries a name")
    return out


def _members(raw: Any, source: str, listed: frozenset[str] | None) -> list[dict]:
    if not isinstance(raw, list):
        raise InvalidCollection("members must be an array")
    if len(raw) > MAX_MEMBERS:
        raise InvalidCollection(f"members holds {len(raw)} entries, at most {MAX_MEMBERS} (MAX_MEMBERS)")
    out: list[dict] = []
    keys: set[str] = set()
    for index, member in enumerate(raw):
        where = f"members[{index}]"
        if not isinstance(member, Mapping):
            raise InvalidCollection(f"{where} must be an object")
        if ("screen" in member) == ("tool" in member):
            raise InvalidCollection(f"{where} names exactly one of screen and tool")
        clean = (
            _screen_member(member, where, source, listed) if "screen" in member else _tool_member(member, where, source)
        )
        try:
            key = member_key(clean)
        except InvalidMember as exc:
            raise InvalidCollection(f"{where}: {exc}") from None
        if key in keys:
            raise InvalidCollection(f"{where} duplicates member_key {key!r}")
        keys.add(key)
        out.append(clean)
    return out


# --- layout ---------------------------------------------------------------------

_REASON_TEXT = {
    "not_layout": "a layout node is a tile (tabs) or a split (split, items)",
    "unknown_field": "unknown field",
    "bad_split": "split must be 'row' or 'column'",
    "bad_tile_id": "tile must be a non-empty string",
    "duplicate_tile": "a tile id is used twice",
    "bad_key": "a tab must name a member by its 0-based index",
    "empty_tabs": "tabs must list at least one tab",
    "duplicate_key": "a tab is placed twice",
    "showing_not_in_tabs": "showing must be one of the tile's tabs",
    "split_too_few_items": "a split holds at least two items",
    "bad_size": "size must be a positive number",
    "unknown_from": "from must name another tile of the layout",
    "member_missing": "every member is placed in a tile",
}


def layout_refusal(layout: Any, *, form: LayoutForm = "dock", members: int | None = None) -> tuple[str, str] | None:
    """``(reason, where)`` of the first layout rule ``layout`` breaks, in tree order; ``None`` when valid.

    ``form`` is ``dock`` (tile ids, string keys, fractions) or ``collection`` (no ids,
    weights, ``showing`` optional). With ``members`` (collection form), every key must be a
    member index below it and every member placed once. The viewer's ``validateLayout``,
    rule for rule: both replay the shared runtime ``validate`` cases.
    """
    try:
        check_layout(layout, form, members)
    except LayoutRefused as refused:
        return refused.reason, refused.where
    except RecursionError:
        return "not_layout", "layout"
    return None


def _layout(layout: Any, members: int) -> Any:
    try:
        size = compact_size(layout)
    except (TypeError, ValueError, RecursionError):
        raise InvalidLayout("not_layout", "layout") from None
    if size > MAX_LAYOUT_BYTES:
        raise InvalidCollection(f"layout exceeds {MAX_LAYOUT_BYTES} bytes (MAX_LAYOUT_BYTES)")
    refusal = layout_refusal(layout, form="collection", members=members)
    if refusal is not None:
        raise InvalidLayout(*refusal)
    return layout


def validate_collection(
    doc: Any,
    *,
    source: Literal["facility", "personal"],
    library: Path | str | Judged | None = None,
    file_id: str | None = None,
) -> dict[str, Any]:
    """``doc`` checked against every rule in the module text, with names stripped.

    ``source`` is the store the document belongs to. ``library``, when given, is the
    library root (or its judgement) that ``library/*`` screen members must be listed in.
    ``file_id`` is the id the store expects (a facility file's stem, a personal PUT's
    URL id): a present ``id`` field must equal it, and it fills a missing one.
    Raises :class:`InvalidCollection` naming the first broken rule, :class:`InvalidLayout`
    for a broken layout.
    """
    if source not in SOURCES:
        raise ValueError(f"source must be one of {SOURCES}")
    if not isinstance(doc, Mapping):
        raise InvalidCollection("a collection must be an object")
    try:
        total = compact_size(doc)
    except (TypeError, ValueError) as exc:
        raise InvalidCollection(f"a collection must be JSON: {exc}") from None
    if total > MAX_COLLECTION_BYTES:
        raise InvalidCollection(
            f"the collection is {total} bytes, at most {MAX_COLLECTION_BYTES} (MAX_COLLECTION_BYTES)"
        )
    _unknown(doc, _COLLECTION_KEYS, "the collection")
    out: dict[str, Any] = {}
    if "id" in doc:
        out["id"] = _id_value(doc["id"], "id")
        if file_id is not None and out["id"] != file_id:
            raise InvalidCollection(f"id {out['id']!r} must equal the file id {file_id!r}")
    elif file_id is not None:
        out["id"] = _id_value(file_id, "the file id")
    if "name" not in doc:
        raise InvalidCollection("name is required")
    out["name"] = _name(doc["name"], "name")
    if "description" in doc:
        out["description"] = _text(doc["description"], "description")
    if "members" not in doc:
        raise InvalidCollection("members is required")
    listed = library_screens(library) if library is not None else None
    out["members"] = _members(doc["members"], source, listed)
    if "layout" in doc:
        out["layout"] = _layout(doc["layout"], len(out["members"]))
    if "default" in doc:
        if source != FACILITY:
            raise InvalidCollection("default is legal only in a facility collection")
        if not isinstance(doc["default"], bool):
            raise InvalidCollection("default must be true or false")
        out["default"] = doc["default"]
    if "updated" in doc:
        out["updated"] = _text(doc["updated"], "updated")
    return out


# --- stores ----------------------------------------------------------------------


class InvalidCollectionId(ValueError):
    """The id does not match ``^[A-Za-z0-9_-]{1,64}$``."""


class InvalidStateOp(ValueError):
    """The state op breaks the op rules; the message names the rule."""


class CollectionNotFound(LookupError):
    """No personal collection is stored under the id."""


class CollectionExists(Exception):
    """A create-only ``PUT`` (``If-None-Match: *``) named an id that is stored (412)."""

    code = "exists"


class CollectionGone(LookupError):
    """A conditional ``PUT`` (``If-Match``) named an id that is not stored (412)."""

    code = "gone"


class CollectionStale(Exception):
    """An ``If-Match`` rev no longer matches the stored collection (412)."""

    code = "stale"


class StateFull(Exception):
    """A state list is at its cap (409); ``code`` names which."""

    code = "state_full"

    def __init__(self, detail: str):
        super().__init__(f"{self.code}: {detail}")
        self.detail = detail


class PinnedFull(StateFull):
    code = "pinned_full"


class SelectionFull(StateFull):
    code = "selection_full"


class StateTooLarge(ValueError):
    """An op exceeds ``MAX_STATE_OP_BYTES`` or the state would exceed ``MAX_STATE_BYTES`` (413)."""


def validate_collection_id(collection_id: Any) -> str:
    if not isinstance(collection_id, str) or not VIEW_ID_PATTERN.fullmatch(collection_id):
        raise InvalidCollectionId(f"invalid collection id: {collection_id!r}")
    return collection_id


def validate_ref(ref: Any) -> str:
    """``ref`` when it is ``<source>:<id>``; raises :class:`InvalidStateOp`."""
    if isinstance(ref, str) and ":" in ref:
        source, collection_id = ref.split(":", 1)
        if source in SOURCES and VIEW_ID_PATTERN.fullmatch(collection_id):
            return ref
    raise InvalidStateOp("ref must be '<facility|personal>:<id>' with an id matching ^[A-Za-z0-9_-]{1,64}$")


def validate_state_member(member: Any) -> dict[str, Any]:
    """``member`` cleaned by the personal member rules. Raises :class:`InvalidStateOp`."""
    if not isinstance(member, Mapping):
        raise InvalidStateOp("member must be an object")
    try:
        (clean,) = _members([member], PERSONAL, None)
    except InvalidCollection as exc:
        raise InvalidStateOp(str(exc).replace("members[0]", "member", 1)) from None
    return clean


def empty_state() -> dict[str, list]:
    return {name: [] for name in STATE_LISTS}


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _mtime(path: Path) -> str:
    stamp = datetime.fromtimestamp(path.stat().st_mtime, UTC)
    return stamp.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _serialize(doc: Mapping[str, Any]) -> bytes:
    return (json.dumps(doc, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def _serialize_state(state: Mapping[str, Any]) -> bytes:
    # Compact, so the stored size is the size the limits are measured in.
    return json.dumps(state, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


# Keys the server adds to a listed collection; a client echoing a listed doc back may
# carry them, so ``put`` strips them before validation. ``rev`` is never stored.
_LISTED_KEYS = frozenset({"source", "ref", "rev", "updated"})


def _listed(doc: dict[str, Any], source: str, data: bytes) -> dict[str, Any]:
    """``doc`` as listed: ``source``, ``ref`` and ``rev``, the first 16 hex of the
    sha256 of the stored file bytes ``data``."""
    rev = hashlib.sha256(data).hexdigest()[:16]
    return {"source": source, "ref": f"{source}:{doc['id']}", "rev": rev, **doc}


def _sorted(docs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(docs, key=lambda doc: (doc["name"].casefold(), doc["id"]))


class _Reader:
    """Reads ``<dir>/<id>.json`` collections of one source, logging each bad file once."""

    source: str

    def __init__(self, directory: Path | str):
        self.dir = Path(directory)
        self._logged: set[tuple[str, int, int]] = set()

    def _skip(self, path: Path, reason: str, verb: str = "skipping") -> None:
        try:
            st = path.stat()
            mark = (str(path), st.st_mtime_ns, st.st_size)
        except OSError:
            mark = (str(path), 0, 0)
        if mark not in self._logged:
            self._logged.add(mark)
            log.warning("%s %s collection %s: %s", verb, self.source, path, reason)

    def _load(self, path: Path, collection_id: str) -> dict[str, Any] | None:
        try:
            data = path.read_bytes()
            doc = json.loads(data)
        except (OSError, ValueError) as exc:
            self._skip(path, f"unreadable: {exc}")
            return None
        try:
            clean = validate_collection(doc, source=self.source, file_id=collection_id)  # type: ignore[arg-type]
        except InvalidCollection as exc:
            self._skip(path, str(exc))
            return None
        if "updated" not in clean:
            try:
                clean["updated"] = _mtime(path)
            except OSError:
                return None
        return _listed(clean, self.source, data)

    def list(self) -> list[dict[str, Any]]:
        """Every readable, valid collection, sorted by case-folded name, then id."""
        if not self.dir.is_dir():
            return []
        docs = []
        for path in self.dir.glob(f"*{COLLECTION_SUFFIX}"):
            collection_id = path.name[: -len(COLLECTION_SUFFIX)]
            if path.name == STATE_FILE or not path.is_file():
                continue
            if not VIEW_ID_PATTERN.fullmatch(collection_id):
                self._skip(path, "the file stem is not a collection id")
                continue
            doc = self._load(path, collection_id)
            if doc is not None:
                docs.append(doc)
        return _sorted(docs)

    def refs(self) -> set[str]:
        """The refs of the valid collections."""
        return {doc["ref"] for doc in self.list()}


class FacilityCollections(_Reader):
    """The read-only facility collections in ``directory`` (id = file stem)."""

    source = FACILITY


def _rev_matches(data: bytes, if_match: frozenset[str]) -> bool:
    """Whether the stored bytes ``data`` have a rev ``if_match`` names (``*``: any)."""
    return "*" in if_match or hashlib.sha256(data).hexdigest()[:16] in if_match


def _parse_if_match(header: str) -> frozenset[str]:
    """The revs an ``If-Match`` header names: comma-separated, each bare, quoted or ``W/``."""
    revs = set()
    for part in header.split(","):
        part = part.strip().removeprefix("W/").strip()
        if len(part) >= 2 and part[0] == part[-1] == '"':
            part = part[1:-1]
        if part:
            revs.add(part)
    return frozenset(revs)


class CollectionStore(_Reader):
    """The personal collections and the per-user state in ``directory``.

    ``facility``, when given, is the facility reader whose refs count as current
    collections when a full ref list evicts. The directory is created on the first write.
    """

    source = PERSONAL

    def __init__(self, directory: Path | str, *, facility: FacilityCollections | None = None):
        super().__init__(directory)
        self.facility = facility
        self._lock = asyncio.Lock()

    @property
    def state_path(self) -> Path:
        return self.dir / STATE_FILE

    def path_for(self, collection_id: str) -> Path:
        return self.dir / f"{validate_collection_id(collection_id)}{COLLECTION_SUFFIX}"

    def get(self, collection_id: str) -> dict[str, Any]:
        path = self.path_for(collection_id)
        doc = self._load(path, collection_id) if path.is_file() else None
        if doc is None:
            raise CollectionNotFound(collection_id)
        return doc

    # -- collections --

    async def put(
        self,
        collection_id: str,
        doc: Any,
        *,
        create_only: bool = False,
        if_match: frozenset[str] | None = None,
        library: Path | str | Judged | None = None,
    ) -> tuple[dict[str, Any], bool]:
        """Validate and store ``doc`` under ``collection_id``; returns ``(listed doc, created)``.

        ``updated`` is set to now; a client-sent ``rev``, ``source`` or ``ref`` (an
        echoed listed doc) is dropped before validation. ``create_only`` (``If-None-Match: *``) raises
        :class:`CollectionExists` when the id is stored. ``if_match`` (the parsed
        ``If-Match`` revs) replaces only the stored rev it names: :class:`CollectionGone`
        when the id is not stored, :class:`CollectionStale` when its rev differs. Raises
        :class:`InvalidCollection` (400) and :class:`ReadOnlyStore` (403).
        """
        path = self.path_for(collection_id)
        if isinstance(doc, Mapping):
            doc = {**{k: v for k, v in doc.items() if k not in _LISTED_KEYS}, "updated": _now()}
        clean = validate_collection(doc, source=PERSONAL, library=library, file_id=collection_id)
        stored = {"id": collection_id, **{k: v for k, v in clean.items() if k != "id"}}
        data = _serialize(stored)
        async with self._lock:
            created = await asyncio.to_thread(self._put_locked, path, data, create_only, if_match)
        return _listed(stored, PERSONAL, data), created

    def _put_locked(self, path: Path, data: bytes, create_only: bool, if_match: frozenset[str] | None = None) -> bool:
        make_dirs(self.dir)
        with lock_root(self.dir):
            created = not path.exists()
            if create_only and not created:
                raise CollectionExists(f"collection {path.stem!r} exists")
            if if_match is not None:
                # Compared under the lock, so no other writer lands between check and write.
                try:
                    current = path.read_bytes()
                except FileNotFoundError:
                    raise CollectionGone(path.stem) from None
                if not _rev_matches(current, if_match):
                    raise CollectionStale(path.stem)
            write_atomic(path, data)
        return created

    async def delete(self, collection_id: str, *, if_match: frozenset[str] | None = None) -> None:
        """Remove the collection and prune ``personal:<id>`` from ``ticked`` and ``unticked``.

        ``if_match`` (the parsed ``If-Match`` revs) removes only the stored rev it names
        (:class:`CollectionStale` otherwise); a missing id is :class:`CollectionNotFound`
        either way.
        """
        path = self.path_for(collection_id)
        async with self._lock:
            await asyncio.to_thread(self._delete_locked, collection_id, path, if_match)

    def _delete_locked(self, collection_id: str, path: Path, if_match: frozenset[str] | None = None) -> None:
        try:
            with lock_root(self.dir):
                if if_match is not None and not _rev_matches(path.read_bytes(), if_match):
                    raise CollectionStale(collection_id)
                remove_file(path)
                state = self._read_state()
                ref = f"{PERSONAL}:{collection_id}"
                if ref in state["ticked"] or ref in state["unticked"]:
                    state["ticked"] = [r for r in state["ticked"] if r != ref]
                    state["unticked"] = [r for r in state["unticked"] if r != ref]
                    self._write_state(state)
        except FileNotFoundError:
            raise CollectionNotFound(collection_id) from None

    # -- state --

    def _read_state(self) -> dict[str, list]:
        """The stored state with every malformed entry dropped; empty when there is none."""
        try:
            raw = json.loads(self.state_path.read_bytes())
        except FileNotFoundError:
            return empty_state()
        except (OSError, ValueError) as exc:
            self._skip(self.state_path, f"unreadable state: {exc}")
            return empty_state()
        if not isinstance(raw, Mapping):
            self._skip(self.state_path, "the state is not an object")
            return empty_state()
        state = empty_state()
        for name in ("ticked", "unticked"):
            for ref in raw.get(name) or []:
                try:
                    ref = validate_ref(ref)
                except InvalidStateOp:
                    continue
                if ref not in state[name] and len(state[name]) < MAX_REFS:
                    state[name].append(ref)
        for name, cap in (("pinned", MAX_PINNED), ("recent", MAX_RECENT)):
            keys: set[str] = set()
            for member in raw.get(name) or []:
                try:
                    clean = validate_state_member(member)
                except InvalidStateOp:
                    continue
                key = member_key(clean)
                if key not in keys and len(state[name]) < cap:
                    keys.add(key)
                    state[name].append(clean)
        return state

    def _write_state(self, state: Mapping[str, Any]) -> None:
        data = _serialize_state(state)
        if len(data) > MAX_STATE_BYTES:
            raise StateTooLarge(f"the state would be {len(data)} bytes, at most {MAX_STATE_BYTES} (MAX_STATE_BYTES)")
        write_atomic(self.state_path, data)

    def state(self) -> dict[str, list]:
        """The state document ``{ticked, unticked, pinned, recent}``."""
        if not self.dir.is_dir():
            return empty_state()
        return self._read_state()

    def _current_refs(self) -> set[str]:
        refs = self.refs()
        if self.facility is not None:
            refs |= self.facility.refs()
        return refs

    async def apply_op(self, op: Any) -> dict[str, list]:
        """Apply one state op (module text) and return the new state document.

        Raises :class:`InvalidStateOp` (400), :class:`StateTooLarge` (413),
        :class:`PinnedFull` / :class:`SelectionFull` (409) and :class:`ReadOnlyStore` (403).
        """
        name, value = _parse_op(op)
        async with self._lock:
            return await asyncio.to_thread(self._apply_locked, name, value)

    def _apply_locked(self, name: str, value: Any) -> dict[str, list]:
        make_dirs(self.dir)
        with lock_root(self.dir):
            state = self._read_state()
            before = json.dumps(state, sort_keys=True)
            if name in ("tick", "untick"):
                into, out = ("ticked", "unticked") if name == "tick" else ("unticked", "ticked")
                state[out] = [ref for ref in state[out] if ref != value]
                if value not in state[into]:
                    if len(state[into]) >= MAX_REFS:
                        current = self._current_refs()
                        state[into] = [ref for ref in state[into] if ref in current]
                    if len(state[into]) >= MAX_REFS:
                        raise SelectionFull(f"{into} holds {MAX_REFS} refs (MAX_REFS)")
                    state[into].append(value)
            elif name == "recent-clear":
                state["recent"] = []
            elif name == "recent-many":
                keys = {member_key(member) for member in value}
                others = [member for member in state["recent"] if member_key(member) not in keys]
                state["recent"] = [*value, *others][:MAX_RECENT]
            else:
                key = member_key(value)
                listed = "recent" if name.startswith("recent") else "pinned"
                others = [member for member in state[listed] if member_key(member) != key]
                if name == "recent":
                    state["recent"] = [value, *others][:MAX_RECENT]
                elif name == "recent-remove":
                    state["recent"] = others
                elif name == "unpin":
                    state["pinned"] = others
                elif len(others) == len(state["pinned"]):
                    if len(state["pinned"]) >= MAX_PINNED:
                        raise PinnedFull(f"pinned holds {MAX_PINNED} members (MAX_PINNED)")
                    state["pinned"].append(value)
            if json.dumps(state, sort_keys=True) != before:
                self._write_state(state)
        return state


def _parse_op(op: Any) -> tuple[str, Any]:
    if not isinstance(op, Mapping):
        raise InvalidStateOp("an op must be an object")
    try:
        size = compact_size(op)
    except (TypeError, ValueError) as exc:
        raise InvalidStateOp(f"an op must be JSON: {exc}") from None
    name = op.get("op")
    cap, cap_name = (
        (MAX_RECENT_MANY_OP_BYTES, "MAX_RECENT_MANY_OP_BYTES")
        if name == "recent-many"
        else (MAX_STATE_OP_BYTES, "MAX_STATE_OP_BYTES")
    )
    if size > cap:
        raise StateTooLarge(f"the op is {size} bytes, at most {cap} ({cap_name})")
    if not isinstance(name, str) or name not in OP_FIELDS:
        raise InvalidStateOp(f"op must be one of {', '.join(STATE_OPS)}")
    field = OP_FIELDS[name]
    expected = {"op"} if field is None else {"op", field}
    if set(op) != expected:
        shape = "{op}" if field is None else f"{{op, {field}}}"
        raise InvalidStateOp(f"a {name} op is exactly {shape}")
    if field is None:
        return name, None
    if field == "ref":
        return name, validate_ref(op["ref"])
    if field == "members":
        return name, _parse_recent_members(op["members"])
    return name, validate_state_member(op["member"])


def _parse_recent_members(members: Any) -> list[dict[str, Any]]:
    """``recent-many``'s members cleaned, first occurrence of each member key kept."""
    if not isinstance(members, list):
        raise InvalidStateOp("members must be an array")
    if len(members) > MAX_RECENT:
        raise InvalidStateOp(f"members holds {len(members)}, at most {MAX_RECENT} (MAX_RECENT)")
    out: list[dict[str, Any]] = []
    keys: set[str] = set()
    for index, member in enumerate(members):
        try:
            clean = validate_state_member(member)
        except InvalidStateOp as exc:
            raise InvalidStateOp(str(exc).replace("member", f"members[{index}]", 1)) from None
        key = member_key(clean)
        if key not in keys:
            keys.add(key)
            out.append(clean)
    return out


def all_collections(facility: FacilityCollections | None, personal: CollectionStore | None) -> list[dict[str, Any]]:
    """Facility collections first, then personal, each sorted by case-folded name, then id."""
    out: list[dict[str, Any]] = []
    for reader in (facility, personal):
        if reader is not None:
            out.extend(reader.list())
    return out


def shown_refs(collections: Iterable[Mapping[str, Any]], state: Mapping[str, Any]) -> list[str]:
    """The sidebar's shown selection: (defaults union ticked) minus unticked, in list order.

    ``collections`` is what :func:`all_collections` returns (or any ``[{ref, default?}]``
    in list order); ``state`` is a collections state, of which only ``ticked`` and
    ``unticked`` are read. Only ``default: true`` counts as a default, unticked wins over
    ticked, and a ticked ref that is not listed adds nothing.
    This is the selection the viewer's ``shownSelection`` computes; both replay ``collections-shown.json``.
    """
    ticked = set(state.get("ticked") or ())
    unticked = set(state.get("unticked") or ())
    return [
        doc["ref"]
        for doc in collections
        if doc["ref"] not in unticked and (doc.get("default") is True or doc["ref"] in ticked)
    ]


# --- HTTP ----------------------------------------------------------------------

_READ_ONLY_DETAIL = "collections cannot be changed here"


_json = json_response
_error = error_response


def _conditional(request: Request, response: Response) -> Response:
    """``response`` with an ``ETag`` (sha256 of its body); 304 on a matching ``If-None-Match``.

    The 304 keeps ``ETag`` and ``Cache-Control: no-cache`` and carries no body.
    """
    tag = etag(hashlib.sha256(response.body).hexdigest())
    if not_modified(request, tag):
        return Response(status_code=304, headers={"ETag": tag, "Cache-Control": COLLECTIONS_CACHE_CONTROL})
    response.headers["ETag"] = tag
    return response


_PRECONDITION_DETAIL = "send If-None-Match: * to create or If-Match: <rev> to replace"


def _not_allowed(allow: str) -> Response:
    return _error(405, "method_not_allowed", f"allowed: {allow}", {"Allow": allow})


def _cross_site(request: Request) -> Response | None:
    if is_cross_site(request):
        return _error(403, "cross_site", "cross-site writes are refused")
    return None


def _read_only() -> Response:
    # A fixed text: the exception names a server path, which a browser has no use for.
    return _error(403, ReadOnlyStore.code, _READ_ONLY_DETAIL)


async def _json_body(request: Request, cap: int) -> tuple[Any, Response | None]:
    """The parsed JSON body, or an error response (415, 413 over ``cap``, 400)."""
    if not is_json_request(request):
        return None, _error(415, "unsupported_media_type", "Content-Type must be application/json")
    raw = await read_capped_body(request, cap)
    if raw is None:
        return None, _error(413, "too_large", f"the body exceeds {cap} bytes")
    try:
        return json.loads(raw), None
    except ValueError:
        return None, _error(400, "invalid_json", "the body is not valid JSON")


def build_collection_routes(personal: CollectionStore, facility: FacilityCollections | None = None) -> list[Route]:
    """``/collections``, ``/collections/{id}`` and ``/collections-state`` over both stores."""

    async def collection(request: Request) -> Response:
        if request.method not in ("GET", "HEAD"):
            return _not_allowed("GET, HEAD")
        listed = await asyncio.to_thread(all_collections, facility, personal)
        return _conditional(request, _json({"collections": listed}))

    async def put(
        request: Request, collection_id: str, *, create_only: bool, if_match: frozenset[str] | None
    ) -> Response:
        body, failed = await _json_body(request, MAX_PUT_BODY_BYTES)
        if failed is not None:
            return failed
        try:
            doc, created = await personal.put(collection_id, body, create_only=create_only, if_match=if_match)
        except InvalidCollection as exc:
            return _error(400, "invalid_collection", str(exc))
        except CollectionExists:
            return _error(412, CollectionExists.code, f"collection {collection_id!r} exists")
        except CollectionGone:
            return _error(412, CollectionGone.code, f"collection {collection_id!r} is gone")
        except CollectionStale:
            return _error(412, CollectionStale.code, f"collection {collection_id!r} has changed")
        except ReadOnlyStore:
            return _read_only()
        return _json(doc, 201 if created else 200)

    async def item(request: Request) -> Response:
        method = request.method
        if method not in ("GET", "HEAD", "PUT", "DELETE"):
            return _not_allowed("GET, HEAD, PUT, DELETE")
        if method in ("PUT", "DELETE") and (refused := _cross_site(request)) is not None:
            return refused
        try:
            collection_id = validate_collection_id(request.path_params["collection_id"])
        except InvalidCollectionId as exc:
            return _error(400, "invalid_id", str(exc))
        header = request.headers.get("if-match")
        if_match = None if header is None else _parse_if_match(header)
        if method == "PUT":
            # A PUT says what it expects: create (If-None-Match: *) or replace the rev it saw (If-Match).
            create_only = if_match is None and request.headers.get("if-none-match", "").strip() == "*"
            if if_match is None and not create_only:
                return _error(428, "precondition_required", _PRECONDITION_DETAIL)
            return await put(request, collection_id, create_only=create_only, if_match=if_match)
        try:
            if method == "DELETE":
                await personal.delete(collection_id, if_match=if_match)
                return Response(status_code=204, headers={"Cache-Control": COLLECTIONS_CACHE_CONTROL})
            return _json(await asyncio.to_thread(personal.get, collection_id))
        except CollectionNotFound:
            return _error(404, "not_found", f"no personal collection {collection_id!r}")
        except CollectionStale:
            return _error(412, CollectionStale.code, f"collection {collection_id!r} has changed")
        except ReadOnlyStore:
            return _read_only()

    async def state(request: Request) -> Response:
        method = request.method
        if method not in ("GET", "HEAD", "POST"):
            return _not_allowed("GET, HEAD, POST")
        if method != "POST":
            return _conditional(request, _json(await asyncio.to_thread(personal.state)))
        if (refused := _cross_site(request)) is not None:
            return refused
        body, failed = await _json_body(request, MAX_POST_BODY_BYTES)
        if failed is not None:
            return failed
        try:
            return _json(await personal.apply_op(body))
        except InvalidStateOp as exc:
            return _error(400, "invalid_op", str(exc))
        except StateTooLarge as exc:
            return _error(413, "too_large", str(exc))
        except StateFull as exc:
            return _error(409, exc.code, exc.detail)
        except ReadOnlyStore:
            return _read_only()

    return [
        Route(f"/{COLLECTIONS_PATH}", collection, methods=ALL_METHODS),
        Route(f"/{COLLECTIONS_PATH}-state", state, methods=ALL_METHODS),
        # {path:path} so ids with a slash (always invalid) answer 400 here, not a bare 404.
        Route(f"/{COLLECTIONS_PATH}/{{collection_id:path}}", item, methods=ALL_METHODS),
    ]
