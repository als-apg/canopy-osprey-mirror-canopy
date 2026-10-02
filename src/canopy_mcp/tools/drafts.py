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
message, ...}`` with ``reason`` one of ``check_errors``, ``stale_rev`` or
``exists``. Bad arguments (invalid id, unknown node, missing draft) raise
:class:`ToolError`. Successful writes return ``{draftId, rev, url, errors,
warnings}``.
"""

from __future__ import annotations

import asyncio
import copy
import re
from collections.abc import Callable, Iterable, Iterator
from pathlib import Path
from typing import Any

from fastmcp.exceptions import ToolError

from canopy_mcp.check.findings import Finding
from canopy_mcp.config import Settings, get_settings
from canopy_mcp.server import mcp
from canopy_mcp.store import DraftNotFound, DraftStore, RevConflict
from canopy_mcp.tools._common import read_draft, refused, validate_id
from canopy_mcp.tools.check import run_check
from canopy_mcp.urls import draft_url

PATCHABLE_KEYS = frozenset({"props", "geometry", "rules", "warnings"})

# One store per drafts directory, so every tool call shares its write lock.
_stores: dict[Path, DraftStore] = {}


def get_store(settings: Settings | None = None) -> DraftStore:
    """The process-wide :class:`DraftStore` for ``settings.drafts_dir``."""
    settings = settings or get_settings()
    key = Path(settings.drafts_dir).resolve()
    store = _stores.get(key)
    if store is None:
        store = _stores[key] = DraftStore(settings.drafts_dir)
    return store


def reset_stores() -> None:
    """Forget every cached store (tests switch drafts directories)."""
    _stores.clear()


# --- helpers ------------------------------------------------------------------------


def _read(store: DraftStore, draft_id: str) -> tuple[dict[str, Any], str]:
    doc, rev = read_draft(store, draft_id)
    if not isinstance(doc, dict):
        raise ToolError(f"draft {draft_id!r} is not a Screen-IR document")
    return doc, rev


def _stale(draft_id: str, base_rev: str | None, current: str | None) -> dict[str, Any]:
    what = "no base_rev given" if not base_rev else f"base_rev {base_rev!r} is stale"
    return refused(
        draft_id,
        "stale_rev",
        f"{what}; the draft is at rev {current!r}. Call get_screen and redo the edit on the current document.",
        currentRev=current,
    )


def _nodes(node: Any) -> Iterator[tuple[dict[str, Any], dict[str, Any] | None]]:
    """Depth-first ``(node, parent)`` over ``node`` and its descendants."""
    stack: list[tuple[Any, Any]] = [(node, None)]
    while stack:
        current, parent = stack.pop()
        if not isinstance(current, dict):
            continue
        yield current, parent
        children = current.get("children")
        if isinstance(children, list):
            stack.extend((child, current) for child in reversed(children))


def _find(doc: dict[str, Any], node_id: str) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """The first node with ``id == node_id`` (document order) and its parent node."""
    for node, parent in _nodes(doc.get("root")):
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
    return {
        "draftId": draft_id,
        "rev": rev,
        "url": draft_url(settings, draft_id),
        "errors": _dicts(preexisting),
        "warnings": _dicts(f for f in new_result.findings if f.severity == "warning"),
    }


#: The SDK's ``ColorRole`` shape, mirrored from ``pydmconverter.scheme`` for when that module is missing.
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

    Uses ``pydmconverter.scheme.stamp_schema_version`` when the installed converter
    has it, and otherwise (for example the PyPI 1.0.5 release) a local walk with
    the same rule, so an edit never fails for want of the converter.
    """
    try:
        from pydmconverter.scheme import stamp_schema_version
    except ImportError:
        return "1.1" if _contains_color_role(doc) else "1.0"
    return stamp_schema_version(doc)


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
    store = get_store(settings)
    validate_id(draft_id)
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
    refuse. Returns ``{draftId, rev, url, errors, warnings}``: open ``url`` to
    see the screen, and pass ``rev`` as ``base_rev`` to the next edit.
    """
    settings = get_settings()
    store = get_store(settings)
    validate_id(id)
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
    """Return draft ``id`` as ``{draftId, rev, url, screen}``; ``rev`` is the ``base_rev`` for edits."""
    settings = get_settings()
    store = get_store(settings)
    validate_id(id)
    doc, rev = _read(store, id)
    return {"draftId": id, "rev": rev, "url": draft_url(settings, id), "screen": doc}


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
    store = get_store()
    validate_id(id)
    current = store.rev(id)
    if current is None:
        raise ToolError(f"no draft stored under {id!r}")
    if not base_rev or base_rev != current:
        return _stale(id, base_rev, current)
    try:
        await store.delete(id, base_rev)
    except RevConflict:
        return _stale(id, base_rev, store.rev(id))
    except DraftNotFound as exc:
        raise ToolError(f"no draft stored under {id!r}") from exc
    return {"draftId": id, "deleted": True}


@mcp.tool
async def list_drafts() -> dict[str, Any]:
    """List every draft as ``{draftId, title, path, rev, url}``."""
    settings = get_settings()
    store = get_store(settings)
    drafts = [
        {
            "draftId": entry["id"],
            "title": entry["title"],
            "path": entry["path"],
            "rev": entry["rev"],
            "url": draft_url(settings, entry["id"]),
        }
        for entry in store.manifest()["screens"]
    ]
    return {"drafts": drafts}
