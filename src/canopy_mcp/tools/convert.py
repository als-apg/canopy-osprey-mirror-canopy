"""The ``convert_legacy`` tool: a legacy panel and its sub-screens into drafts, plus a work queue.

One call converts the parent file and the sub-screens its converted IR embeds
or opens, each through ``POST /converter/v1/conversions`` (synchronous, one
file per request), and writes them as drafts beside each other:

* **Paths.** ``path`` and every ``siblings`` source path must be absolute after
  ``~`` expansion; otherwise the call refuses, naming the key, before any
  conversion is posted.
* **Ref matching by key.** Each ref of the converted PARENT is matched to a
  source by key (:mod:`canopy_mcp.refs`): a ``siblings`` entry, else the default
  source ``realpath(dirname(path)/key.<kind>)`` when that file exists. A ref that
  is absolute, climbs above the drafts root, is not a valid draft id, or has no
  source is simply not written. Only the parent's refs are followed.
* **Placement and stamping.** The parent is written at ``id``, each sibling at
  ``normalize(dirname(id)/key)``; each document's ``id`` is set to its draft id
  and ``metadata.title`` to the source file stem (the last id segment for
  ``content``), so the output is byte-deterministic.
* **Revs, all or nothing.** Converted documents skip the FR4 check gate (the
  converter schema-validated them). A draft whose bytes are identical is reused
  untouched; any other existing draft needs ``overwrite=true`` and its current
  rev in ``base_revs``, else the WHOLE call refuses before any write and lists
  the conflicting ids.
* **Image copy.** After the drafts are written, every image ref
  (``image-view.src``, ``pv-button.onImage/offImage``) of every written or
  reused draft is copied from beside its legacy source into the store at
  ``screens/<relpath>``, ``relpath`` resolved against the draft's directory.
  Each ref yields exactly one ``assets[]`` entry with its status; the store
  (:meth:`DraftStore.write_asset`) is the single enforcement point. Nothing is
  raised once the drafts are committed.
* **Queue.** For every draft written or reused: ``check(draftId)`` findings
  limited to :data:`QUEUE_CODES`, plus the conversion's ``droppedProps`` as
  ``dropped_prop`` items. It is measured after the image copy, so a copied
  image leaves no ``unresolved_ref``; neither the ref classification above nor
  the copy emits queue items itself.
* **Scheme guard.** Each conversion's ``scheme`` (the color scheme the
  converter applied) is kept; the parent's is returned as ``scheme``. When
  ``CANOPY_THEME_FILE`` is set, ``overwrite=true`` refuses before any write
  unless every unit reports a ``scheme``, they all agree, it is not
  ``canopy-default``-served, carries no ``errors``, and its ``(id, sha256)``
  equal this server's own ``colorScheme.meta``: overwriting a themed draft
  with a conversion made against another scheme would silently re-color it.
"""

from __future__ import annotations

import os
import stat
from typing import Any
from pathlib import Path, PurePosixPath
from dataclasses import field, dataclass

import httpx
from fastmcp.exceptions import ToolError

from canopy_mcp.refs import SOURCE_KINDS, ir_ref_key, sibling_key, source_kind
from canopy_mcp.urls import draft_url
from canopy_mcp.store import (
    MAX_ASSET_BYTES,
    DraftStore,
    RevConflict,
    AssetRefused,
    doc_rev,
)
from canopy_mcp.client import make_client
from canopy_mcp.config import Settings, get_settings
from canopy_mcp.server import mcp
from canopy_mcp.check.refs import (
    dirname,
    iter_refs,
    resolve_ref,
    iter_image_refs,
    resolve_image_ref,
)
from canopy_mcp.tools.check import run_check
from canopy.core.color_scheme import DEFAULT_ID, THEME_FILE_ENV, get_color_scheme
from canopy_mcp.tools._common import route, refused, read_only_refusal

CONVERSIONS_PATH = "/converter/v1/conversions"
CONVERT_TIMEOUT_S = 60.0
KINDS = frozenset(SOURCE_KINDS.values())

# The check codes that go to the queue; inert_in_osprey and lint findings stay in check only.
QUEUE_CODES: tuple[str, ...] = ("warning", "unknown_widget", "unresolved_ref", "nested_ref_base")


@dataclass
class _Unit:
    """One file to convert and the draft it becomes."""

    draft_id: str
    kind: str
    filename: str
    data: bytes
    title: str
    doc: dict[str, Any] = field(default_factory=dict)
    dropped: list[dict[str, Any]] = field(default_factory=list)
    source_path: str | None = None  # realpath of the legacy file; None in content mode
    scheme: dict[str, Any] | None = None  # the response's ``scheme``; None when absent or null

    @classmethod
    def from_file(cls, draft_id: str, kind: str, path: str) -> _Unit:
        """The unit for the legacy source at ``path`` (read now), titled by its file stem."""
        return cls(
            draft_id,
            kind,
            os.path.basename(path),
            _read_source(path),
            PurePosixPath(path).stem,
            source_path=os.path.realpath(path),
        )


def _expand(raw: Any) -> str | None:
    """``raw`` after ``~`` expansion when that is absolute, else ``None``."""
    if not isinstance(raw, str) or not raw:
        return None
    expanded = os.path.expanduser(raw)
    return expanded if os.path.isabs(expanded) else None


def _read_source(path: str) -> bytes:
    try:
        return Path(path).read_bytes()
    except OSError as exc:
        raise ToolError(f"cannot read legacy source {path!r}: {exc}") from exc


def _kind_of(kind: str | None, path: str | None) -> str:
    if kind is None and path is not None:
        kind = SOURCE_KINDS.get(PurePosixPath(path).suffix.lower())
    if kind not in KINDS:
        raise ToolError(f"kind must be one of {sorted(KINDS)} (or inferable from the path suffix), got {kind!r}")
    return kind


async def _post(client: httpx.AsyncClient, unit: _Unit) -> None:
    """Convert ``unit`` through the backend and keep its IR and dropped props."""
    try:
        response = await client.post(
            CONVERSIONS_PATH,
            files={"file": (unit.filename, unit.data, "application/octet-stream")},
            data={"kind": unit.kind},
        )
    except httpx.HTTPError as exc:
        raise ToolError(f"conversion of {unit.filename!r} failed: {exc!r}") from exc
    if response.status_code != 200:
        raise ToolError(f"conversion of {unit.filename!r} failed: HTTP {response.status_code}: {response.text[:500]}")
    body = response.json()
    screen = body.get("screen")
    if not isinstance(screen, dict):
        raise ToolError(f"conversion of {unit.filename!r} returned no screen document")
    screen["id"] = unit.draft_id
    metadata = screen.get("metadata")
    if not isinstance(metadata, dict):
        metadata = screen["metadata"] = {}
    metadata["title"] = unit.title
    unit.doc = screen
    scheme = body.get("scheme")
    unit.scheme = scheme if isinstance(scheme, dict) else None
    unit.dropped = [
        {"draftId": unit.draft_id, "nodeId": d.get("nodeId"), "code": "dropped_prop", "props": list(d.get("props", []))}
        for d in body.get("droppedProps") or []
        if isinstance(d, dict)
    ]


def _scheme_problem(units: list[_Unit]) -> str | None:
    """Why the units' conversions must not overwrite drafts under ``CANOPY_THEME_FILE``, or ``None``."""
    missing = [u.draft_id for u in units if u.scheme is None]
    if missing:
        return f"the converter reported no scheme for {', '.join(map(repr, missing))}"
    first = units[0].scheme
    assert first is not None
    differing = [u.draft_id for u in units[1:] if u.scheme != first]
    if differing:
        return f"the conversions of {', '.join(map(repr, differing))} used a different scheme than the parent's"
    if first.get("served") == DEFAULT_ID:
        return f"the converter served {DEFAULT_ID!r} instead of the configured scheme"
    if first.get("errors"):
        return f"the converter's scheme has {len(first['errors'])} error(s)"
    own = get_color_scheme().meta
    if (first.get("id"), first.get("sha256")) != (own.get("id"), own.get("sha256")):
        return (
            f"the converter's scheme (id={first.get('id')!r}, sha256={first.get('sha256')!r}) differs from "
            f"this server's (id={own.get('id')!r}, sha256={own.get('sha256')!r})"
        )
    return None


def _sibling_sources(siblings: dict[str, str]) -> dict[str, str]:
    """``{key: absolute source path}`` for every relative ``siblings`` key (paths already checked)."""
    out: dict[str, str] = {}
    for raw, source in siblings.items():
        key = sibling_key(raw)
        if key is None:
            continue  # an absolute key never matches a written ref
        expanded = _expand(source)
        assert expanded is not None
        if key in out and out[key] != expanded:
            raise ToolError(f"siblings keys that normalize to {key!r} name different source files")
        out[key] = expanded
    return out


def _siblings_of(parent: _Unit, parent_id: str, path: str | None, kind: str, sources: dict[str, str]) -> list[_Unit]:
    """The sibling units the parent's refs resolve to (first appearance order, de-duplicated)."""
    units: list[_Unit] = []
    seen = {parent_id}
    base = dirname(parent_id)
    for _node_id, _path, raw, _embedded in iter_refs(parent.doc):
        target, reason = resolve_ref(raw, base)
        if reason is not None or target is None or target in seen:
            continue  # absolute, above_root, invalid_id: not written (check reports it)
        key = ir_ref_key(raw)
        assert key is not None
        source = sources.get(key)
        if source is None and path is not None:
            candidate = os.path.realpath(os.path.join(os.path.dirname(path), f"{key}.{kind}"))
            source = candidate if os.path.isfile(candidate) else None
        if source is None:
            continue  # missing: not written (check reports it)
        seen.add(target)
        units.append(_Unit.from_file(target, source_kind(source, kind), source))
    return units


# ``detail`` for the statuses that carry no exception message.
_DETAIL = {
    "above_root": "the ref climbs above the drafts root; nothing was copied",
    "invalid_ref": "the ref cannot name a stored image (a scheme, macro or character outside [A-Za-z0-9 ._-], "
    "or no image suffix); nothing was copied",
    "no_source": "converted from content, so there is no source directory to copy the image from",
    "missing": "no regular file at the source path beside the legacy file",
    "too_large": f"the source exceeds the {MAX_ASSET_BYTES}-byte asset limit; nothing was copied",
    "conflict": "an earlier ref in this call already stored different bytes at this path; not written",
}


def _asset(
    unit: _Unit, ref: str, path: str | None, size: int | None, status: str, detail: str | None = None
) -> dict[str, Any]:
    out: dict[str, Any] = {"draftId": unit.draft_id, "ref": ref, "path": path, "bytes": size, "status": status}
    if status not in ("written", "reused", "replaced", "external"):
        out["detail"] = detail or _DETAIL[status]
    return out


async def _copy_one(
    unit: _Unit, ref: str, store: DraftStore, overwrite: bool, seen: dict[str, bytes]
) -> dict[str, Any]:
    """Copy one image ref of ``unit`` into the store and report it (FR6 lookup order)."""
    relpath, status = resolve_image_ref(ref, dirname(unit.draft_id))
    if relpath is None:
        return _asset(unit, ref, None, None, status or "invalid_ref")
    path = f"screens/{relpath}"
    if unit.source_path is None:
        return _asset(unit, ref, path, None, "no_source")
    source = os.path.realpath(os.path.join(os.path.dirname(unit.source_path), ref))
    try:
        st = os.stat(source)
    except FileNotFoundError:
        return _asset(unit, ref, path, None, "missing")
    except OSError as exc:
        return _asset(unit, ref, path, None, "error", str(exc))
    if not stat.S_ISREG(st.st_mode):
        return _asset(unit, ref, path, None, "missing")
    size = st.st_size
    if size > MAX_ASSET_BYTES:
        return _asset(unit, ref, path, size, "too_large")
    try:
        with open(source, "rb") as fh:
            data = fh.read(MAX_ASSET_BYTES + 1)
    except OSError as exc:
        return _asset(unit, ref, path, size, "error", str(exc))
    if len(data) > MAX_ASSET_BYTES:
        return _asset(unit, ref, path, size, "too_large")
    size = len(data)
    if relpath in seen:  # already stored by this call: never let a later ref replace it
        return _asset(unit, ref, path, size, "reused" if seen[relpath] == data else "conflict")
    try:
        written = await store.write_asset(relpath, data, overwrite=overwrite)
    except AssetRefused as exc:
        return _asset(unit, ref, path, size, exc.reason, str(exc))
    except OSError as exc:
        return _asset(unit, ref, path, size, "error", str(exc))
    seen[relpath] = data
    return _asset(unit, ref, path, size, written)


async def _copy_images(units: list[_Unit], store: DraftStore, overwrite: bool) -> list[dict[str, Any]]:
    """One ``assets[]`` entry per image ref of every unit, in unit then document order."""
    seen: dict[str, bytes] = {}
    return [
        await _copy_one(unit, ref, store, overwrite, seen)
        for unit in units
        for _node_id, _path, ref in iter_image_refs(unit.doc)
    ]


async def _queue(units: list[_Unit], store: DraftStore, settings: Settings) -> list[dict[str, Any]]:
    queue: list[dict[str, Any]] = []
    for unit in units:
        result = await run_check(draft_id=unit.draft_id, store=store, settings=settings)
        queue.extend({"draftId": unit.draft_id, **f.to_dict()} for f in result.findings if f.code in QUEUE_CODES)
        queue.extend(unit.dropped)
    return queue


@mcp.tool
async def convert_legacy(
    id: str,
    path: str | None = None,
    content: str | None = None,
    kind: str | None = None,
    siblings: dict[str, str] | None = None,
    overwrite: bool = False,
    base_revs: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Convert a legacy panel (``.bob``, ``.opi``, ``.edl``, ``.ui``) and the sub-screens it references into drafts.

    Give ``id`` (the parent's draft id, e.g. ``bts/BTSView``) and either
    ``path`` (an absolute file path; ``~`` is expanded) or ``content`` (the
    file text), with ``kind`` ``bob|edl|ui`` (inferred from ``path``'s suffix
    when omitted). ``siblings`` maps a sub-screen ref AS WRITTEN in the legacy
    file (e.g. ``embedded/_KnobDevice.bob``) to its absolute source path; a ref
    with no entry is looked for beside ``path`` as ``<ref>.<kind>``. Only the
    parent's refs are followed. Each sub-screen is written at
    ``dirname(id)/<ref>``; refs that are absolute, climb above the drafts root,
    are not valid draft ids or have no source are not written and show up in
    ``queue`` as ``unresolved_ref`` with that ``reason``.

    Converted drafts are written without the check gate. Re-running is safe:
    identical drafts are reused. An existing draft with different content needs
    ``overwrite=true`` and ``base_revs={draftId: rev}``; otherwise the whole
    call refuses, lists ``conflicts`` and writes nothing. Relative paths refuse
    naming the offending key.

    Images (``image-view.src``, ``pv-button.onImage/offImage``) are copied
    from beside each legacy source into the store next to the draft that
    references them; ``overwrite=true`` also replaces a differing stored image.

    When ``CANOPY_THEME_FILE`` is set, ``overwrite=true`` refuses
    (``reason: scheme_mismatch``, nothing written) unless every conversion
    reports the same scheme, served without errors, whose ``id`` and
    ``sha256`` match this server's.

    Returns ``{draftId, rev, url, drafts, assets, queue, scheme}``: ``scheme``
    is the converter's ``{id, sha256, served, errors}`` for the parent
    (``null`` when it reported none); ``drafts`` lists
    every draft written or reused with its ``rev``; ``assets`` has one entry
    ``{draftId, ref, path, bytes, status, detail?}`` per image ref, ``path``
    the served ``screens/...`` path (``null`` when the ref has none), ``bytes``
    the source size when known, ``status`` one of ``written``, ``reused``,
    ``replaced``, ``external`` (a URL or ``/`` ref, left alone) or a failure --
    ``missing``, ``above_root``, ``invalid_ref``, ``too_large``, ``not_image``,
    ``no_source`` (``content`` given, nowhere to copy from), ``conflict``
    (something differing is already stored there) or ``error`` -- which carries
    a ``detail``. ``queue`` is the work left for
    you, every item carrying ``draftId`` (and ``docId`` for a node of an
    embedded screen): ``check(draftId)`` findings with code ``warning``
    (converter notes; clear one with ``patch_node`` ``set:{warnings:[]}``),
    ``unknown_widget`` (with ``originalClass``; replace with ``remove_node`` +
    ``add_node``), ``unresolved_ref`` and ``nested_ref_base``, plus
    ``dropped_prop`` items ``{nodeId, props}`` naming legacy properties the IR
    could not carry. ``dropped_prop`` names mix source vocabulary (``.bob`` /
    ``.edl`` attribute names dropped by the source adapters) and Qt vocabulary
    (Qt property names dropped by the IR builder, the ``.ui`` path). Running
    ``check(draftId)`` re-measures the same queue.
    """
    settings = get_settings()
    store, _ = route(id, settings)
    if refusal := read_only_refusal(id, store):
        return refusal
    if (path is None) == (content is None):
        raise ToolError("give exactly one of path (an absolute file path) or content (the legacy file text)")
    siblings = siblings or {}
    base_revs = base_revs or {}
    if not isinstance(siblings, dict) or not all(isinstance(k, str) for k in siblings):
        raise ToolError("siblings must map a ref as written to an absolute source path")

    # Refuse relative paths before anything is read or posted.
    relative = []
    abs_path: str | None = None
    if path is not None:
        abs_path = _expand(path)
        if abs_path is None:
            relative.append("path")
    relative.extend(key for key, source in siblings.items() if _expand(source) is None)
    if relative:
        return refused(
            id,
            "relative_path",
            f"refused: {', '.join(map(repr, relative))} must be an absolute path (after ~ expansion); nothing was converted",
            keys=relative,
        )

    kind = _kind_of(kind, abs_path)
    sources = _sibling_sources(siblings)
    if abs_path is not None:
        parent = _Unit.from_file(id, kind, abs_path)
    else:
        last = id.rsplit("/", 1)[-1]
        parent = _Unit(id, kind, f"{last}.{kind}", str(content).encode("utf-8"), last)

    async with make_client(settings, timeout=CONVERT_TIMEOUT_S) as client:
        await _post(client, parent)
        # A ref that lands on an id the store reserves is not written (check reports it).
        siblings_found = _siblings_of(parent, id, abs_path, kind, sources)
        units = [parent, *(unit for unit in siblings_found if not store.reserves(unit.draft_id))]
        for unit in units[1:]:
            await _post(client, unit)

    if overwrite and os.environ.get(THEME_FILE_ENV, "").strip():
        problem = _scheme_problem(units)
        if problem is not None:
            return refused(
                id,
                "scheme_mismatch",
                f"refused: overwrite needs a conversion made with this server's color scheme, but {problem}. "
                "Nothing was written.",
                scheme=parent.scheme,
            )

    # Decide every draft's fate before writing any.
    to_write: dict[str, tuple[dict[str, Any], str | None]] = {}
    revs: dict[str, str] = {}
    conflicts: list[str] = []
    for unit in units:
        new_rev = doc_rev(unit.doc)
        current = store.rev(unit.draft_id)
        if current is None:
            to_write[unit.draft_id] = (unit.doc, None)
        elif current == new_rev:
            revs[unit.draft_id] = current  # identical bytes: reuse, do not rewrite
        elif overwrite and base_revs.get(unit.draft_id) == current:
            to_write[unit.draft_id] = (unit.doc, current)
        else:
            conflicts.append(unit.draft_id)
    if conflicts:
        return _conflict(id, conflicts, overwrite)
    if to_write:
        try:
            revs.update(await store.write_many(to_write))
        except RevConflict as exc:
            return _conflict(id, exc.conflicts, overwrite)

    assets = await _copy_images(units, store, overwrite)
    queue = await _queue(units, store, settings)
    return {
        "draftId": id,
        "rev": revs[id],
        "url": draft_url(settings, id),
        "drafts": [
            {"draftId": u.draft_id, "rev": revs[u.draft_id], "url": draft_url(settings, u.draft_id)} for u in units
        ],
        "assets": assets,
        "queue": queue,
        "scheme": parent.scheme,
    }


def _conflict(draft_id: str, conflicts: list[str], overwrite: bool) -> dict[str, Any]:
    how = "pass base_revs with each draft's current rev" if overwrite else "pass overwrite=true and base_revs"
    return refused(
        draft_id,
        "conflict",
        f"refused: {len(conflicts)} existing draft(s) differ from the conversion; {how}. Nothing was written.",
        conflicts=conflicts,
    )
