from typing import Any, Literal
from datetime import datetime

from canopy.contracts.base import CanopyModel


class JobStatusDTO(CanopyModel):
    """Status of a background job, reconstructed from Arq state + a Redis progress key.

    ``result`` is an opaque reference produced by the task (e.g.
    ``{"type": "snapshot", "id": ...}``); the Jobs module never interprets it,
    which is what keeps Jobs domain-agnostic.
    """

    id: str
    function: str | None = None
    status: Literal["pending", "running", "completed", "failed"]
    progress: int = 0
    message: str | None = None
    result: Any | None = None
    error: str | None = None
    enqueued_at: datetime | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
