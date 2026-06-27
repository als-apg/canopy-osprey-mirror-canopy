"""Plugin for site-specific writes that must accompany a snapshot restore.

Some labs need extra puts beyond the snapshot's setpoint write — e.g. magnets that
require a TRIM put to a CTRL PV before they physically move. A lab implements
:class:`RestoreHook`, registers the subclass in
``canopy.squirrel.restore_hooks.registry.RESTORE_HOOK_PROVIDERS``, and selects it via
``CANOPY_SQUIRREL__RESTORE_HOOK``.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class RestoreHookError(Exception):
    """Raised by a hook when an address it recognises is malformed.

    Treated by ``SnapshotService.restore_snapshot`` as a per-PV failure: the primary
    setpoint write is skipped and the error is surfaced in the restore result's
    failures (so the system is never left in an inconsistent state).
    """


class RestoreHook(ABC):
    """Abstract base class for restore-time hooks."""

    @abstractmethod
    def derive_extra_writes(self, address: str) -> list[tuple[str, Any]]:
        """Return additional ``(address, value)`` puts to send *after* the snapshot's
        write to ``address`` succeeds.

        Returns an ordered list of extra puts; an empty list means nothing extra for
        this PV. Raises :class:`RestoreHookError` when the hook recognises the address
        but cannot derive the extras (e.g. a malformed name) — the restore then treats
        this PV as failed without attempting the primary write.
        """
