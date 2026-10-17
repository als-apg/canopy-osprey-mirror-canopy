"""Collections: named, ordered sets of things the viewer can open.

This text is the format of a collection file and the one rule set every reader and
writer applies (:func:`validate_collection`): the personal store's ``PUT``, the reader
of both stores and ``--check``. The viewer applies the same rules in ``collections.ts``.

A collection is a JSON object::

    {
      "id": "<VIEW_ID_PATTERN>",
      "name": "<1..120 characters>",
      "description"?: "<text>",
      "members": [<member>, ...],
      "layout"?: <layout>,
      "default"?: true | false,
      "updated": "<ISO-8601>"
    }

No other key is allowed, here or in a member or a layout node.

- ``id`` matches ``^[A-Za-z0-9_-]{1,64}$``. A facility collection's id is its file
  stem, so the field may be left out; when present it must equal the stem. A personal
  id is minted once by the client and taken from the URL. The store passes the id it
  expects as ``file_id``.
- ``name``: a string, stripped, 1 to 120 characters, no control characters.
- ``description``: a string, without lone surrogates.
- ``updated``: a string when present; a facility file may leave it out (its mtime
  stands in), the personal store writes it.
- ``default``: a boolean, legal only in a facility collection. A collection with
  ``default: true`` is shown to every user who has not unticked it.

A member is one of two kinds (identity and key grammar: :mod:`canopy_mcp.member_key`)::

    {"screen": "<category>/<id>", "macros"?: {"<name>": "<value>", ...}, "slot"?: "<slot>"}
    {"tool": "<tool id>", "query"?: "<opaque>", "name"?: "<label>", "slot"?: "<slot>"}

- ``<category>`` is ``library`` or ``drafts``; ``<id>`` matches
  ``[A-Za-z0-9_-]+(/[A-Za-z0-9_-]+)*``. A facility collection names ``library/*``
  screens only; a personal one may also name ``drafts/*``.
- macro names and values are strings.
- ``<tool id>`` is one of :data:`canopy_mcp.static.TOOL_IDS`: the static tuple, not the
  tools this host serves, so a host with fewer tools never drops a file. A missing
  ``query`` means the tool's config default, ``""`` a blank tool; a query is opaque
  text of at most ``MAX_QUERY_BYTES`` (4096) UTF-8 bytes. Whether it parses is the
  viewer's concern.
- ``name`` follows the collection ``name`` rule. Every tool member of a facility
  collection carries one.
- No two members share a ``member_key``.
- ``slot`` matches ``VIEW_ID_PATTERN``; no two members share one.

A layout places slotted members in a dock grid::

    {"split": "row" | "column", "items": [<item>, ...], "active"?: "<slot>"}
    <item> = {"slot": "<slot>", "size"?: <weight>} | {"items": [<item>, ...], "size"?: <weight>}

Only the root names ``split`` (and ``active``); nested splits alternate direction
implicitly. Every split holds at least two items, ``size`` is a positive finite relative
weight, a split nests at most ``MAX_LAYOUT_DEPTH`` (6) deep counting the root, and every
leaf names the slot of a member, each slot at most once. ``active`` names a member's
slot. A member without a leaf is legal: it opens undocked.

Limits: at most ``MAX_MEMBERS`` (50) members, the layout at most ``MAX_LAYOUT_BYTES``
(32 KiB) and the whole document at most ``MAX_COLLECTION_BYTES`` (256 KiB), each
measured as compact UTF-8 JSON.

With ``library`` (a library root, or its :class:`~canopy_mcp.listing.Judged`), every
``library/<id>`` member must be a screen the library lists:
``screens/<id>.screen.json`` among the listed paths and no refusal naming it or a
directory above it. Images never match, by their suffix.

Stores
------

The facility store (:class:`FacilityCollections`) is a read-only directory of
``<id>.json`` files: the id is the file stem, a missing ``updated`` reads as the file's
mtime, and a file that is unreadable or breaks a rule is logged once (until it
changes) and skipped. The personal store (:class:`CollectionStore`) keeps
``<personal dir>/<id>.json`` with the rules of :mod:`canopy_mcp.views`: every write
is one section under an exclusive ``flock`` on the personal directory, each file is
replaced atomically, an unwritable directory raises
:class:`~canopy_mcp.store.ReadOnlyStore` (``read_only_store``). A listed collection
carries ``source``, ``ref`` (``<source>:<id>``) and ``rev``.

``rev`` is the first 16 hex digits of the sha256 of the stored file bytes: it changes
whenever the file does and is never stored itself. A client that echoes a listed
collection back may leave ``source``, ``ref``, ``rev`` and ``updated`` in it; ``PUT``
drops them before validation. Every personal write states what it expects, and the
store compares under the lock, so no other writer lands between check and write:

| Request | Outcome | Refusal |
| --- | --- | --- |
| ``PUT``, neither header | refused | 428 ``precondition_required`` |
| ``PUT`` + ``If-None-Match: *`` | create only | 412 ``exists``: the id is stored |
| ``PUT`` + ``If-Match: <rev>`` | replace that rev only | 412 ``gone``: id not stored; 412 ``stale``: another rev |
| ``DELETE`` + ``If-Match: <rev>`` | remove that rev only | 412 ``stale``: another rev; 404 ``not_found`` |
| ``DELETE``, no ``If-Match`` | remove whatever is stored | 404 ``not_found`` |

The details read "send If-None-Match: * to create or If-Match: <rev> to replace",
"collection '<id>' exists", "collection '<id>' is gone" and "collection '<id>' has
changed". ``If-Match`` names one or more revs, comma-separated, each bare, quoted
or ``W/``; ``*`` matches any stored rev. When both headers are sent, ``If-Match`` decides.

The per-user state is ``<personal dir>/.state.json`` (its stem is outside
``VIEW_ID_PATTERN``, so it is never a collection)::

    {"ticked": [<ref>, ...], "unticked": [<ref>, ...], "pinned": [<member>, ...], "recent": [<member>, ...]}

and changes only through one op at a time, applied read-modify-write under the same
lock (:meth:`CollectionStore.apply_op`)::

    {"op": "tick" | "untick", "ref": "<source>:<id>"}
    {"op": "pin" | "unpin" | "recent", "member": <member>}

``tick`` adds the ref to ``ticked`` and drops it from ``unticked``; ``untick`` the
reverse. ``pin`` appends a member whose ``member_key`` is not pinned yet, ``unpin``
drops the one that has its key; ``recent`` moves the member to the front, dropping any
other with its key, and keeps the first ``MAX_RECENT`` (10). All are idempotent. A
member here follows the personal member rules without ``slot``. Bounds: an op is at
most ``MAX_STATE_OP_BYTES`` (8 KiB), the stored state at most ``MAX_STATE_BYTES``
(512 KiB), both as compact UTF-8 JSON; ``MAX_PINNED`` (50) pinned members, at most
``MAX_REFS`` (100) refs per list. A full list first evicts the refs naming no current
collection, then raises :class:`SelectionFull` (409 ``selection_full``); a full
``pinned`` raises :class:`PinnedFull` (409 ``pinned_full``). Deleting a personal
collection prunes ``personal:<id>`` from both ref lists under the same lock.

HTTP surface
------------

:func:`build_collection_routes`, every response ``Cache-Control: no-cache``:

* ``GET /collections`` -> 200 ``{"collections": [...]}``: facility first, then personal,
  each sorted by case-folded name, then id, each carrying ``source`` and ``ref``
* ``GET /collections/{id}`` -> 200 the personal collection, 404 ``not_found``
* ``PUT /collections/{id}`` with a JSON collection body -> 201 created, 200 replaced
  (body: the listed collection); ``If-None-Match: *`` makes it create-only (412
  ``exists``). A ``default`` key, or any broken rule, is 400 ``invalid_collection``
* ``DELETE /collections/{id}`` -> 204, 404 ``not_found``
* ``GET /collections-state`` -> 200 the state document
* ``POST /collections-state`` with one JSON op -> 200 the new state document; 400
  ``invalid_op``, 409 ``pinned_full`` / ``selection_full``, 413 ``too_large``

``PUT``, ``DELETE`` and ``POST`` act on the personal store only. Every error body is
``{"error": "<code>", "detail"?: "<text>"}``; besides the codes above: 400
``invalid_id`` (also an id with a slash), 400 ``invalid_json``, 403 ``read_only_store``
(the personal directory may not be written; the detail is fixed text, never a server
path), 403 ``cross_site`` (a write whose ``Sec-Fetch-Site`` is present and is neither
``same-origin`` nor ``none``; reads are never guarded), 405 ``method_not_allowed``
(with ``Allow``), 413 ``too_large`` over :data:`MAX_PUT_BODY_BYTES` /
:data:`MAX_POST_BODY_BYTES`, 415 ``unsupported_media_type`` unless ``Content-Type:
application/json``. As for :mod:`canopy_mcp.views`, the routes carry no auth of their
own: they sit behind the app's proxy guard.
"""

from __future__ import annotations

import json
import math
import hashlib
import asyncio
import logging
from typing import Any, Literal
from pathlib import Path
from datetime import UTC, datetime
from collections.abc import Mapping

from starlette.routing import Route
from starlette.requests import Request
from starlette.responses import Response, JSONResponse

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
from canopy_mcp.views import (
    ALL_METHODS,
    MAX_QUERY_BYTES,
    VIEW_ID_PATTERN,
    ALLOWED_FETCH_SITES,
    InvalidView,
    clean_name,
    check_query,
)
from canopy_mcp.static import TOOL_IDS
from canopy_mcp.listing import Judged, scan, judge
from canopy_mcp.member_key import InvalidMember, member_key

__all__ = [
    "FACILITY",
    "PERSONAL",
    "SOURCES",
    "MAX_MEMBERS",
    "MAX_LAYOUT_DEPTH",
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
    "validate_collection",
    "validate_collection_id",
    "validate_ref",
    "validate_state_member",
]

FACILITY = "facility"
PERSONAL = "personal"
SOURCES = (FACILITY, PERSONAL)
MAX_MEMBERS = 50
MAX_LAYOUT_DEPTH = 6
MAX_LAYOUT_BYTES = 32 * 1024
MAX_COLLECTION_BYTES = 256 * 1024
SPLITS = ("row", "column")
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
COLLECTIONS_CACHE_CONTROL = "no-cache"
STATE_FILE = ".state.json"
STATE_LISTS = ("ticked", "unticked", "pinned", "recent")
STATE_OPS = ("tick", "untick", "pin", "unpin", "recent")

log = logging.getLogger(__name__)

_COLLECTION_KEYS = frozenset({"id", "name", "description", "members", "layout", "default", "updated"})
_SCREEN_KEYS = frozenset({"screen", "macros", "slot"})
_TOOL_KEYS = frozenset({"tool", "query", "name", "slot"})
_ROOT_KEYS = frozenset({"split", "items", "active"})
_NODE_KEYS = frozenset({"items", "size"})
_LEAF_KEYS = frozenset({"slot", "size"})
_SCREENS_PREFIX = "screens/"


class InvalidCollection(ValueError):
    """The document breaks a collection rule; the message names the field and the rule."""


def _size(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8", "surrogatepass"))


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
    except InvalidView as exc:
        raise InvalidCollection(f"{where}: {exc}") from None


def _slot(value: Any, where: str) -> str:
    if not isinstance(value, str) or not VIEW_ID_PATTERN.fullmatch(value):
        raise InvalidCollection(f"{where} must match ^[A-Za-z0-9_-]{{1,64}}$")
    return value


def _unknown(doc: Mapping[str, Any], allowed: frozenset[str], where: str) -> None:
    extra = sorted(str(key) for key in doc if key not in allowed)
    if extra:
        raise InvalidCollection(f"{where} has unknown key(s): {', '.join(extra)}")


def library_screens(library: Path | str | Judged) -> frozenset[str]:
    """The screen ids (``<id>`` of ``library/<id>``) a library root or its judgement lists."""
    judged = library if isinstance(library, Judged) else judge(scan(Path(library)))
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
        except InvalidView as exc:
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
    slots: set[str] = set()
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
        if "slot" in member:
            slot = _slot(member["slot"], f"{where}.slot")
            if slot in slots:
                raise InvalidCollection(f"{where}.slot {slot!r} is not unique")
            slots.add(slot)
            clean["slot"] = slot
        out.append(clean)
    return out


def _weight(node: Mapping[str, Any], where: str) -> None:
    if "size" not in node:
        return
    size = node["size"]
    finite = isinstance(size, int) or (isinstance(size, float) and math.isfinite(size))
    if isinstance(size, bool) or not finite or size <= 0:
        raise InvalidCollection(f"{where}.size must be a positive number")


def _items(node: Mapping[str, Any], where: str, depth: int, slots: set[str], placed: set[str]) -> None:
    if depth > MAX_LAYOUT_DEPTH:
        raise InvalidCollection(f"{where}: layout nests deeper than {MAX_LAYOUT_DEPTH}")
    items = node.get("items")
    if not isinstance(items, list) or len(items) < 2:
        raise InvalidCollection(f"{where}.items must be an array of at least two items")
    for index, item in enumerate(items):
        at = f"{where}.items[{index}]"
        if not isinstance(item, Mapping):
            raise InvalidCollection(f"{at} must be an object")
        if "split" in item or "active" in item:
            raise InvalidCollection(f"{at}: only the layout root names split and active")
        if "slot" in item:
            if "items" in item:
                raise InvalidCollection(f"{at} is either a leaf (slot) or a split (items), not both")
            _unknown(item, _LEAF_KEYS, at)
            slot = item["slot"]
            if not isinstance(slot, str) or slot not in slots:
                raise InvalidCollection(f"{at}.slot must name the slot of a member")
            if slot in placed:
                raise InvalidCollection(f"{at}.slot {slot!r} is placed twice")
            placed.add(slot)
        elif "items" in item:
            _unknown(item, _NODE_KEYS, at)
            _items(item, at, depth + 1, slots, placed)
        else:
            raise InvalidCollection(f"{at} must carry slot or items")
        _weight(item, at)


def _layout(layout: Any, slots: set[str]) -> None:
    if not isinstance(layout, Mapping):
        raise InvalidCollection("layout must be an object")
    _unknown(layout, _ROOT_KEYS, "layout")
    if layout.get("split") not in SPLITS:
        raise InvalidCollection("layout.split must be 'row' or 'column'")
    if _size(layout) > MAX_LAYOUT_BYTES:
        raise InvalidCollection(f"layout exceeds {MAX_LAYOUT_BYTES} bytes (MAX_LAYOUT_BYTES)")
    _items(layout, "layout", 1, slots, set())
    if "active" in layout and (not isinstance(layout["active"], str) or layout["active"] not in slots):
        raise InvalidCollection("layout.active must name the slot of a member")


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
    Raises :class:`InvalidCollection` naming the first broken rule.
    """
    if source not in SOURCES:
        raise ValueError(f"source must be one of {SOURCES}")
    if not isinstance(doc, Mapping):
        raise InvalidCollection("a collection must be an object")
    try:
        total = _size(doc)
    except (TypeError, ValueError) as exc:
        raise InvalidCollection(f"a collection must be JSON: {exc}") from None
    if total > MAX_COLLECTION_BYTES:
        raise InvalidCollection(
            f"the collection is {total} bytes, at most {MAX_COLLECTION_BYTES} (MAX_COLLECTION_BYTES)"
        )
    _unknown(doc, _COLLECTION_KEYS, "the collection")
    out: dict[str, Any] = {}
    if "id" in doc:
        out["id"] = _slot(doc["id"], "id")
        if file_id is not None and out["id"] != file_id:
            raise InvalidCollection(f"id {out['id']!r} must equal the file id {file_id!r}")
    elif file_id is not None:
        out["id"] = _slot(file_id, "the file id")
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
        _layout(doc["layout"], {member["slot"] for member in out["members"] if "slot" in member})
        out["layout"] = doc["layout"]
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
    """``member`` cleaned by the personal member rules; no ``slot``. Raises :class:`InvalidStateOp`."""
    if not isinstance(member, Mapping):
        raise InvalidStateOp("member must be an object")
    if "slot" in member:
        raise InvalidStateOp("member: a pinned or recent member carries no slot")
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

    def _skip(self, path: Path, reason: str) -> None:
        try:
            st = path.stat()
            mark = (str(path), st.st_mtime_ns, st.st_size)
        except OSError:
            mark = (str(path), 0, 0)
        if mark not in self._logged:
            self._logged.add(mark)
            log.warning("skipping %s collection %s: %s", self.source, path, reason)

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
            else:
                key = member_key(value)
                listed = "recent" if name == "recent" else "pinned"
                others = [member for member in state[listed] if member_key(member) != key]
                if name == "recent":
                    state["recent"] = [value, *others][:MAX_RECENT]
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
        size = _size(op)
    except (TypeError, ValueError) as exc:
        raise InvalidStateOp(f"an op must be JSON: {exc}") from None
    if size > MAX_STATE_OP_BYTES:
        raise StateTooLarge(f"the op is {size} bytes, at most {MAX_STATE_OP_BYTES} (MAX_STATE_OP_BYTES)")
    name = op.get("op")
    if name not in STATE_OPS:
        raise InvalidStateOp(f"op must be one of {', '.join(STATE_OPS)}")
    field = "ref" if name in ("tick", "untick") else "member"
    if set(op) != {"op", field}:
        raise InvalidStateOp(f"a {name} op is exactly {{op, {field}}}")
    if field == "ref":
        return name, validate_ref(op["ref"])
    return name, validate_state_member(op["member"])


def all_collections(facility: FacilityCollections | None, personal: CollectionStore | None) -> list[dict[str, Any]]:
    """Facility collections first, then personal, each sorted by case-folded name, then id."""
    out: list[dict[str, Any]] = []
    for reader in (facility, personal):
        if reader is not None:
            out.extend(reader.list())
    return out


# --- HTTP ----------------------------------------------------------------------

_READ_ONLY_DETAIL = "collections cannot be changed here"


def _json(body: Any, status_code: int = 200, headers: dict[str, str] | None = None) -> Response:
    return JSONResponse(
        body, status_code=status_code, headers={"Cache-Control": COLLECTIONS_CACHE_CONTROL, **(headers or {})}
    )


def _error(status_code: int, code: str, detail: str | None = None, headers: dict[str, str] | None = None) -> Response:
    body = {"error": code} if detail is None else {"error": code, "detail": detail}
    return _json(body, status_code, headers)


_PRECONDITION_DETAIL = "send If-None-Match: * to create or If-Match: <rev> to replace"


def _not_allowed(allow: str) -> Response:
    return _error(405, "method_not_allowed", f"allowed: {allow}", {"Allow": allow})


def _cross_site(request: Request) -> Response | None:
    site = request.headers.get("sec-fetch-site")
    if site is not None and site.strip().lower() not in ALLOWED_FETCH_SITES:
        return _error(403, "cross_site", "cross-site writes are refused")
    return None


def _read_only() -> Response:
    # A fixed text: the exception names a server path, which a browser has no use for.
    return _error(403, ReadOnlyStore.code, _READ_ONLY_DETAIL)


async def _json_body(request: Request, cap: int) -> tuple[Any, Response | None]:
    """The parsed JSON body, or an error response (415, 413 over ``cap``, 400)."""
    media = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if media != "application/json":
        return None, _error(415, "unsupported_media_type", "Content-Type must be application/json")
    too_large = _error(413, "too_large", f"the body exceeds {cap} bytes")
    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            if int(declared) > cap:
                return None, too_large
        except ValueError:
            pass
    chunks, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > cap:
            return None, too_large
        chunks.append(chunk)
    try:
        return json.loads(b"".join(chunks)), None
    except ValueError:
        return None, _error(400, "invalid_json", "the body is not valid JSON")


def build_collection_routes(personal: CollectionStore, facility: FacilityCollections | None = None) -> list[Route]:
    """``/collections``, ``/collections/{id}`` and ``/collections-state`` over both stores."""

    async def collection(request: Request) -> Response:
        if request.method not in ("GET", "HEAD"):
            return _not_allowed("GET, HEAD")
        listed = await asyncio.to_thread(all_collections, facility, personal)
        return _json({"collections": listed})

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
            return _json(await asyncio.to_thread(personal.state))
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
        Route("/collections", collection, methods=ALL_METHODS),
        Route("/collections-state", state, methods=ALL_METHODS),
        # {path:path} so ids with a slash (always invalid) answer 400 here, not a bare 404.
        Route("/collections/{collection_id:path}", item, methods=ALL_METHODS),
    ]


if __name__ == "__main__":
    # ``python -m canopy_mcp.collections <dir> --check``: the command lives in collections_cli.
    import sys

    from canopy_mcp.collections_cli import main

    sys.exit(main())
