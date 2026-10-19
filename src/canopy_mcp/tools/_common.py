"""Id routing, store-to-``ToolError`` mapping and the refusal shape the tools share.

Every tool that takes an id resolves it with :func:`route`, which picks the
store and the id inside it. Without a library (``CANOPY_LIBRARY_DIR`` unset)
that is always the personal store and the id as given. With one, ``library/<id>``
is screen ``<id>`` of the read-only library store, and the personal store
refuses ``library`` as a first segment.

Bad arguments raise :class:`ToolError`; refusals are returned, not raised, as
``{draftId, refused: true, reason, message, ...}``. An authoring tool given a
library id returns the ``read_only_store`` refusal (:func:`read_only_refusal`)
before it reads, checks, converts or writes anything. A personal store whose
tree turns out not to be writable gives the same reason at the write
(:func:`unwritable_refusal`).
"""

from __future__ import annotations

from typing import Any
from pathlib import Path

from fastmcp.exceptions import ToolError

from canopy_mcp.urls import split_id
from canopy_mcp.store import (
    LIBRARY,
    DraftStore,
    DraftNotFound,
    ReadOnlyStore,
    InvalidDraftId,
    open_stores,
)
from canopy_mcp.config import Settings, get_settings
from canopy_mcp.workspace import Hub

# One pair of stores per (drafts directory, library directory), so every tool
# call shares the personal store's write lock.
_stores: dict[tuple[Path, Path | None], tuple[DraftStore, DraftStore | None]] = {}


def _open(settings: Settings) -> tuple[DraftStore, DraftStore | None]:
    library_dir = settings.library_dir
    key = (Path(settings.drafts_dir).resolve(), Path(library_dir).resolve() if library_dir is not None else None)
    pair = _stores.get(key)
    if pair is None:
        pair = _stores[key] = open_stores(settings.drafts_dir, library_dir)
    return pair


def get_store(settings: Settings | None = None) -> DraftStore:
    """The process-wide personal :class:`DraftStore` for ``settings.drafts_dir``."""
    return _open(settings or get_settings())[0]


def get_library_store(settings: Settings | None = None) -> DraftStore | None:
    """The process-wide read-only store for ``settings.library_dir``, or ``None`` without a library."""
    return _open(settings or get_settings())[1]


def reset_stores() -> None:
    """Forget every cached store (tests switch directories)."""
    _stores.clear()


# The one workspace hub of this process (in memory, ephemeral; see canopy_mcp.workspace).
_hub: Hub | None = None


def get_hub() -> Hub:
    """The process-wide workspace :class:`Hub`, minted on first use."""
    global _hub
    if _hub is None:
        _hub = Hub()
    return _hub


def reset_hub() -> None:
    """Close the hub (its waiters return ``pending``, its streams end) and forget it; the next
    :func:`get_hub` mints a fresh one with a new epoch. The served app's shutdown and tests call it."""
    global _hub
    hub, _hub = _hub, None
    if hub is not None:
        hub.close()


def validate_id(draft_id: str) -> str:
    """``draft_id`` when it is a valid draft id, else :class:`ToolError`."""
    try:
        return DraftStore.validate_id(draft_id)
    except InvalidDraftId as exc:
        raise ToolError(str(exc)) from exc


def route(draft_id: str, settings: Settings | None = None) -> tuple[DraftStore, str]:
    """``(store, local id)`` for the id a tool was given; :class:`ToolError` when it is not valid there.

    ``library/<id>`` routes to the library store as ``<id>`` when a library is
    configured. Every other id routes to the personal store unchanged, which then
    refuses a reserved first segment (the bare id ``library``).
    """
    settings = settings or get_settings()
    personal, library = _open(settings)
    category, local_id = split_id(settings, draft_id)
    store = library if category == LIBRARY and library is not None else personal
    try:
        return store, store.check_id(local_id)
    except InvalidDraftId as exc:
        raise ToolError(str(exc)) from exc


def read_draft(store: DraftStore, draft_id: str, name: str | None = None) -> tuple[Any, str]:
    """The stored draft and its rev; :class:`ToolError` when it is missing or not JSON.

    ``name`` is the id the caller gave, when it differs from the id inside ``store``;
    the error names that one.
    """
    name = draft_id if name is None else name
    try:
        return store.read(draft_id)
    except DraftNotFound as exc:
        raise ToolError(f"no draft stored under {name!r}") from exc
    except ValueError as exc:
        raise ToolError(f"draft {name!r} is not valid JSON: {exc}") from exc


def refused(draft_id: str, reason: str, message: str, **extra: Any) -> dict[str, Any]:
    """A refusal: nothing was written, and ``reason`` says why."""
    return {"draftId": draft_id, "refused": True, "reason": reason, "message": message, **extra}


def read_only_refusal(draft_id: str, store: DraftStore) -> dict[str, Any] | None:
    """The ``read_only_store`` refusal for a write to ``draft_id``, or ``None`` when ``store`` takes writes."""
    if not store.read_only:
        return None
    detail = f"{draft_id!r} is a {store.category} screen and cannot be changed here; copy it into your own drafts"
    return refused(draft_id, ReadOnlyStore.code, str(ReadOnlyStore(detail)))


def unwritable_refusal(draft_id: str) -> dict[str, Any]:
    """The ``read_only_store`` refusal for a store that raised :class:`ReadOnlyStore` at the write.

    The text is fixed, so no server path reaches the caller.
    """
    detail = "this server's drafts directory cannot be written; nothing was changed"
    return refused(draft_id, ReadOnlyStore.code, str(ReadOnlyStore(detail)))
