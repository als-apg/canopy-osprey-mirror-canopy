from typing import Literal
from datetime import datetime

from canopy.contracts.base import CanopyModel

RestoreHooksState = Literal["disabled", "active", "unknown", "partial"]


class RestoreHooksViewDTO(CanopyModel):
    """What one process's ``CANOPY_SQUIRREL__RESTORE_HOOKS`` resolves to.

    ``status`` is ``"disabled"`` when nothing is configured, ``"active"`` when every
    configured provider key is registered, ``"unknown"`` when none is, and
    ``"partial"`` when only some are. ``loaded`` holds the keys that will actually run,
    ``unknown`` the ones that match no provider, and ``warnings`` configuration
    problems that change nothing but deserve attention.
    """

    status: RestoreHooksState
    configured: list[str] = []
    loaded: list[str] = []
    unknown: list[str] = []
    warnings: list[str] = []


class WorkerRestoreHooksViewDTO(RestoreHooksViewDTO):
    """The worker's view, as it published it when it last started.

    ``reported_at`` and ``host`` identify that start, so a report left behind by a
    worker that is no longer running can be recognised.
    """

    reported_at: datetime
    host: str


class RestoreHooksStatusDTO(CanopyModel):
    """Restore hooks as the API and the worker each resolved them.

    Restores run in the worker, so ``worker`` is the view that decides what a restore
    does; ``api`` is this process's. ``worker`` is ``None`` until a worker has started
    and published its report, or when Redis could not be read (``worker_error`` says
    which). ``matches_api`` is ``None`` without a worker report, and otherwise says
    whether both processes load the same hooks. ``available`` lists every registered
    provider key.
    """

    available: list[str] = []
    api: RestoreHooksViewDTO
    worker: WorkerRestoreHooksViewDTO | None = None
    worker_error: str | None = None
    matches_api: bool | None = None
