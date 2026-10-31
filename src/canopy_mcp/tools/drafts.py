"""Draft tools: create, read, edit and delete the draft screens the viewer shows.

Every write is gated twice:

* **Check gate.** The new document is checked with :func:`run_check` against
  the draft id it will be saved under, and so is the stored document it
  replaces (an id with no stored draft has an empty base). A write is refused
  only for error findings the edit *introduces* -- errors whose
  ``(nodeId, code, path)`` is absent from the base -- so an agent can work on
  a converted screen that already carries errors. A refusal lists those
  errors and writes nothing; errors already present are returned on success.
* **Rev gate.** Edits name the ``base_rev`` they were made against. A stale or
  missing ``base_rev`` refuses, and because checking is slow the store
  re-verifies the rev under its lock when it writes, so a draft changed by a
  human in the viewer meanwhile is never overwritten.

Refusals are returned, not raised: ``{draftId, refused: true, reason,
message, ...}`` with ``reason`` one of ``check_errors``, ``stale_rev``,
``exists`` or ``read_only_store``. Bad arguments (invalid id, unknown node,
missing draft) raise :class:`ToolError`. Successful writes return ``{draftId, rev, url, ref,
errors, warnings}``.

Beside each ``url``, the tools that name a screen return its ``ref``
(``drafts/<id>`` or ``library/<id>``): the reference the workspace ``open``
command takes, formatted by :func:`canopy_mcp.tools._common.screen_ref`.

With a library configured (``CANOPY_LIBRARY_DIR``) the id ``library/<id>`` names
a screen of the read-only library: ``get_screen`` reads it, ``list_drafts``
lists it beside the drafts, and every writing tool refuses it with
``read_only_store`` before anything else happens.
"""

from __future__ import annotations

import re
import copy
import asyncio
from typing import Any
from collections.abc import Callable, Iterable

from fastmcp.exceptions import ToolError

from canopy_mcp.urls import draft_url
from canopy_mcp.store import DraftStore, RevConflict, DraftNotFound, ReadOnlyStore
from canopy_mcp.config import Settings, get_settings
from canopy_mcp.server import mcp
from canopy_mcp.tools.check import run_check
from canopy_mcp.tools._common import (
    route,
    refused,
    draft_ref,
    get_store,
    read_draft,
    get_library_store,
    read_only_refusal,
    unwritable_refusal,
)
from canopy_mcp.check.findings import Finding
from canopy_mcp.check.semantic import iter_nodes

PATCHABLE_KEYS = frozenset({"props", "geometry", "rules", "warnings"})


# --- helpers ------------------------------------------------------------------------


def _read(store: DraftStore, draft_id: str, name: str | None = None) -> tuple[dict[str, Any], str]:
    doc, rev = read_draft(store, draft_id, name)
    if not isinstance(doc, dict):
        raise ToolError(f"draft {name or draft_id!r} is not a Screen-IR document")
    return doc, rev


def _stale(draft_id: str, base_rev: str | None, current: str | None) -> dict[str, Any]:
    what = "no base_rev given" if not base_rev else f"base_rev {base_rev!r} is stale"
    return refused(
        draft_id,
        "stale_rev",
        f"{what}; the draft is at rev {current!r}. Call get_screen and redo the edit on the current document.",
        currentRev=current,
    )


def _find(doc: dict[str, Any], node_id: str) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """The first node with ``id == node_id`` (document order) and its parent node."""
    for _path, node, parent in iter_nodes(doc, parents=True):
        if node.get("id") == node_id:
            return node, parent
    raise ToolError(f"no node with id {node_id!r} in this draft")


async def _gated_write(
    store: DraftStore,
    settings: Settings,
    draft_id: str,
    new: dict[str, Any],
    base: dict[str, Any] | None,
    expect_rev: str | None,
) -> dict[str, Any]:
    """Run the check gate on ``new`` against ``base``, then write under the store's rev check."""
    if base is None:
        new_result = await run_check(new, draft_id, store=store, settings=settings)
        base_keys: set[tuple[str | None, str, str]] = set()
    else:
        new_result, base_result = await asyncio.gather(
            run_check(new, draft_id, store=store, settings=settings),
            run_check(base, draft_id, store=store, settings=settings),
        )
        base_keys = {f.key for f in base_result.errors}
    introduced = [f for f in new_result.errors if f.key not in base_keys]
    preexisting = [f for f in new_result.errors if f.key in base_keys]
    if introduced:
        return refused(
            draft_id,
            "check_errors",
            f"refused: the edit introduces {len(introduced)} error(s); nothing was written",
            errors=_dicts(introduced),
            preexisting=_dicts(preexisting),
        )
    try:
        rev = await store.write(draft_id, new, expect_rev)
    except RevConflict:
        return _stale(draft_id, expect_rev, store.rev(draft_id))
    except ReadOnlyStore:
        return unwritable_refusal(draft_id)
    return {
        "draftId": draft_id,
        "rev": rev,
        "url": draft_url(settings, draft_id),
        "ref": draft_ref(settings, draft_id),
        "errors": _dicts(preexisting),
        "warnings": _dicts(f for f in new_result.findings if f.severity == "warning"),
    }


#: The SDK's ``ColorRole`` shape, the rule ``pydmconverter.scheme`` stamps by (a test pins the two together).
_ROLE_KEYS = frozenset({"$role", "fallback", "fallbackDark"})
_ROLE_HEX = re.compile(r"#([0-9a-fA-F]{6}|[0-9a-fA-F]{8})")
_ROLE_MAX_LENGTH = 64


def _is_color_role(node: Any) -> bool:
    if not isinstance(node, dict) or not set(node) <= _ROLE_KEYS:
        return False
    role = node.get("$role")
    if not isinstance(role, str) or not 1 <= len(role.encode("utf-16-le")) // 2 <= _ROLE_MAX_LENGTH:
        return False

    def is_hex(value: Any) -> bool:
        return isinstance(value, str) and _ROLE_HEX.fullmatch(value) is not None

    return is_hex(node.get("fallback")) and ("fallbackDark" not in node or is_hex(node["fallbackDark"]))


def _contains_color_role(node: Any) -> bool:
    if _is_color_role(node):
        return True
    if isinstance(node, dict):
        return any(_contains_color_role(value) for value in node.values())
    if isinstance(node, list | tuple):
        return any(_contains_color_role(value) for value in node)
    return False


def _stamp_schema_version(doc: Any) -> str:
    """``"1.1"`` iff ``doc`` holds a well-formed colour role, else ``"1.0"``; never raises.

    The converter is an optional dependency, so the rule is walked here rather
    than imported from ``pydmconverter.scheme``.
    """
    return "1.1" if _contains_color_role(doc) else "1.0"


def _stamp(doc: dict[str, Any]) -> None:
    """Set ``doc["schemaVersion"]`` from its content, so the check gate validates the stamped version."""
    doc["schemaVersion"] = _stamp_schema_version(doc)


def _merge_node(target: Any, patch: Any) -> Any:
    """RFC 7396 merge (as :func:`canopy_mcp.merge.merge_patch`), except that a patch value carrying ``$role`` replaces its target whole.

    A colour role is one value, not an object to merge into: merging a role over
    another would keep a stale ``fallbackDark``, and over a hex it is a plain swap.
    """
    if not isinstance(patch, dict) or "$role" in patch:
        return copy.deepcopy(patch)
    result = dict(target) if isinstance(target, dict) else {}
    for key, value in patch.items():
        if value is None:
            result.pop(key, None)
        elif isinstance(value, dict) and "$role" not in value:
            result[key] = _merge_node(result.get(key), value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def _dicts(findings: Iterable[Finding]) -> list[dict[str, Any]]:
    return [f.to_dict() for f in findings]


async def _edit(draft_id: str, base_rev: str, edit: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
    """Read the draft, verify ``base_rev``, apply ``edit(doc)`` to a copy and write it through the gate."""
    settings = get_settings()
    store, _ = route(draft_id, settings)
    if refusal := read_only_refusal(draft_id, store):
        return refusal
    base, current = _read(store, draft_id)
    new = copy.deepcopy(base)
    edit(new)  # raises ToolError on bad arguments, before any rev refusal
    if not base_rev or base_rev != current:
        return _stale(draft_id, base_rev, current)
    _stamp(new)
    return await _gated_write(store, settings, draft_id, new, base, base_rev)


# --- tools --------------------------------------------------------------------------


@mcp.tool
async def new_screen(
    id: str, screen: dict[str, Any], overwrite: bool = False, base_rev: str | None = None
) -> dict[str, Any]:
    """Save a whole Screen-IR document as draft ``id`` (e.g. ``rf/status``).

    Refused, and nothing written, when the document has error findings (see
    ``check``); the refusal lists each error with ``nodeId`` and ``path``.
    Replacing an existing draft needs ``overwrite=true`` and the draft's
    current ``base_rev``; then only errors not already in the stored draft
    refuse. Returns ``{draftId, rev, url, ref, errors, warnings}``: open ``url``
    to see the screen (or pass ``ref`` to the workspace ``open`` command), and
    pass ``rev`` as ``base_rev`` to the next edit.
    """
    settings = get_settings()
    store, _ = route(id, settings)
    if refusal := read_only_refusal(id, store):
        return refusal
    current = store.rev(id)
    base: dict[str, Any] | None = None
    if current is not None:
        if not overwrite:
            return refused(
                id,
                "exists",
                f"draft {id!r} already exists; pass overwrite=true with base_rev, or edit it with patch_node",
                currentRev=current,
            )
        if not base_rev or base_rev != current:
            return _stale(id, base_rev, current)
        try:
            stored, _ = store.read(id)
            base = stored if isinstance(stored, dict) else {}
        except ValueError:
            base = {}  # unreadable stored draft: no base errors to excuse
        except DraftNotFound:
            return _stale(id, base_rev, None)
    elif base_rev:
        return _stale(id, base_rev, None)
    if isinstance(screen, dict):
        screen = copy.deepcopy(screen)  # never stamp the caller's document
        _stamp(screen)
    return await _gated_write(store, settings, id, screen, base, base_rev if current is not None else None)


@mcp.tool
async def get_screen(id: str) -> dict[str, Any]:
    """Return draft ``id`` as ``{draftId, rev, url, ref, screen}``; ``rev`` is the ``base_rev`` for edits.

    ``ref`` is the reference the workspace ``open`` command takes for this screen.

    ``library/<id>`` reads a screen of the shared library, which no tool can change.
    """
    settings = get_settings()
    store, local_id = route(id, settings)
    doc, rev = _read(store, local_id, id)
    return {"draftId": id, "rev": rev, "url": draft_url(settings, id), "ref": draft_ref(settings, id), "screen": doc}


@mcp.tool
async def patch_node(screen_id: str, node_id: str, base_rev: str, set: dict[str, Any]) -> dict[str, Any]:
    """Change one node of draft ``screen_id`` by JSON Merge Patch (RFC 7396).

    ``set`` may hold ``props``, ``geometry``, ``rules`` and ``warnings``. Objects
    merge key by key and ``null`` deletes a key; lists replace, so
    ``{"warnings": []}`` clears a node's converter notes and ``rules`` is the
    full new rule list. ``type``, ``id`` and ``children`` cannot be patched:
    use ``remove_node`` + ``add_node``. Refused on a stale ``base_rev`` or when
    the edit introduces an error. Returns ``{draftId, rev, url, errors, warnings}``.
    """
    if not isinstance(set, dict) or not set:
        raise ToolError(f"set must be a non-empty object with keys from {sorted(PATCHABLE_KEYS)}")
    bad = sorted(k for k in set if k not in PATCHABLE_KEYS)
    if bad:
        raise ToolError(f"cannot patch {bad}; set may only hold {sorted(PATCHABLE_KEYS)}")

    def edit(doc: dict[str, Any]) -> None:
        node, _ = _find(doc, node_id)
        patched = _merge_node(node, set)
        node.clear()
        node.update(patched)

    return await _edit(screen_id, base_rev, edit)


@mcp.tool
async def add_node(screen_id: str, parent_id: str, node: dict[str, Any], base_rev: str) -> dict[str, Any]:
    """Append ``node`` (a full widget node with a unique ``id``) to the children of ``parent_id``.

    Refused on a stale ``base_rev`` or when the node introduces an error (for
    example a duplicate id). Returns ``{draftId, rev, url, errors, warnings}``.
    """
    if not isinstance(node, dict):
        raise ToolError("node must be a widget node object")

    def edit(doc: dict[str, Any]) -> None:
        parent, _ = _find(doc, parent_id)
        children = parent.get("children")
        if not isinstance(children, list):
            children = parent["children"] = []
        children.append(copy.deepcopy(node))

    return await _edit(screen_id, base_rev, edit)


@mcp.tool
async def remove_node(screen_id: str, node_id: str, base_rev: str) -> dict[str, Any]:
    """Remove node ``node_id`` (and its children) from draft ``screen_id``; the root cannot be removed.

    Refused on a stale ``base_rev`` or when the removal introduces an error.
    Returns ``{draftId, rev, url, errors, warnings}``.
    """

    def edit(doc: dict[str, Any]) -> None:
        target, parent = _find(doc, node_id)
        if parent is None:
            raise ToolError("the root node cannot be removed")
        parent["children"] = [child for child in parent["children"] if child is not target]

    return await _edit(screen_id, base_rev, edit)


@mcp.tool
async def delete_draft(id: str, base_rev: str) -> dict[str, Any]:
    """Delete draft ``id``; ``base_rev`` must be its current rev. Returns ``{draftId, deleted: true}``."""
    store, _ = route(id, get_settings())
    if refusal := read_only_refusal(id, store):
        return refusal
    current = store.rev(id)
    if current is None:
        raise ToolError(f"no draft stored under {id!r}")
    if not base_rev or base_rev != current:
        return _stale(id, base_rev, current)
    try:
        await store.delete(id, base_rev)
    except RevConflict:
        return _stale(id, base_rev, store.rev(id))
    except ReadOnlyStore:
        return unwritable_refusal(id)
    except DraftNotFound as exc:
        raise ToolError(f"no draft stored under {id!r}") from exc
    return {"draftId": id, "deleted": True}


@mcp.tool
async def list_drafts() -> dict[str, Any]:
    """List every draft as ``{draftId, title, description, path, rev, url, ref}``.

    ``description`` is the first sentence of the screen's ``metadata.description``
    (at most 200 characters), ``""`` when it has none: use it with ``title`` to pick
    the screen the user means. ``ref`` (``drafts/<id>`` or ``library/<id>``) is what
    the workspace ``open`` command takes; ``draftId`` is what the other draft tools take.

    Screens of the shared library follow the drafts, with ``draftId``
    ``library/<id>``; they can be read, checked and screenshotted, not changed.
    """
    settings = get_settings()
    listed = [(entry["id"], entry) for entry in get_store(settings).manifest(descriptions=True)["screens"]]
    library = get_library_store(settings)
    if library is not None:
        listed += [
            (f"{library.category}/{entry['id']}", entry) for entry in library.manifest(descriptions=True)["screens"]
        ]
    drafts = [
        {
            "draftId": tool_id,
            "title": entry["title"],
            "description": entry["description"],
            "path": entry["path"],
            "rev": entry["rev"],
            "url": draft_url(settings, tool_id),
            "ref": draft_ref(settings, tool_id),
        }
        for tool_id, entry in listed
    ]
    return {"drafts": drafts}
