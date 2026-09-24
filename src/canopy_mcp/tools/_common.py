"""Store-to-``ToolError`` mapping and the refusal shape the write tools share.

Bad arguments raise :class:`ToolError`; refusals are returned, not raised, as
``{draftId, refused: true, reason, message, ...}``.
"""

from __future__ import annotations

from typing import Any

from fastmcp.exceptions import ToolError

from canopy_mcp.store import DraftNotFound, DraftStore, InvalidDraftId


def validate_id(draft_id: str) -> str:
    """``draft_id`` when it is a valid draft id, else :class:`ToolError`."""
    try:
        return DraftStore.validate_id(draft_id)
    except InvalidDraftId as exc:
        raise ToolError(str(exc)) from exc


def read_draft(store: DraftStore, draft_id: str) -> tuple[Any, str]:
    """The stored draft and its rev; :class:`ToolError` when it is missing or not JSON."""
    try:
        return store.read(draft_id)
    except DraftNotFound as exc:
        raise ToolError(f"no draft stored under {draft_id!r}") from exc
    except ValueError as exc:
        raise ToolError(f"draft {draft_id!r} is not valid JSON: {exc}") from exc


def refused(draft_id: str, reason: str, message: str, **extra: Any) -> dict[str, Any]:
    """A refusal: nothing was written, and ``reason`` says why."""
    return {"draftId": draft_id, "refused": True, "reason": reason, "message": message, **extra}
