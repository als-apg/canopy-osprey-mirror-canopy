from typing import Any, Literal
from datetime import datetime

from canopy.contracts.base import CanopyModel


class JobPhaseDTO(CanopyModel):
    """One named stage of a job, with its own progress.

    The task names its phases and the Jobs module never interprets them. ``current``
    and ``total`` count the phase's units of work (EPICS writes, for a restore), while
    ``failures`` counts whatever the task reports failing in it (PVs, for a restore,
    so it matches the restore's failure rows grouped by ``phase``). The two units can
    differ: a hook deriving two writes per PV shows ``failures`` in PVs beside a
    ``total`` in writes.

    A phase with nothing to do is ``skipped``. Upstream react-squirrel's UI only
    knows ``pending`` / ``running`` / ``completed``, so a port of it needs that case.
    """

    name: str
    current: int = 0
    total: int = 0
    status: Literal["pending", "running", "completed", "skipped"] = "pending"
    failures: int = 0


class JobStatusDTO(CanopyModel):
    """Status of a background job, reconstructed from Arq state + a Redis progress key.

    ``result`` is an opaque reference produced by the task (e.g.
    ``{"type": "snapshot", "id": ...}``); the Jobs module never interprets it,
    which is what keeps Jobs domain-agnostic. ``phases`` lists the job's named stages
    when its task reports them, and stays empty otherwise; a finished job keeps its
    final phases for as long as its progress key lives.
    """

    id: str
    function: str | None = None
    status: Literal["pending", "running", "completed", "failed"]
    progress: int = 0
    message: str | None = None
    phases: list[JobPhaseDTO] = []
    result: Any | None = None
    error: str | None = None
    enqueued_at: datetime | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
