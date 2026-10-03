"""Grace period manager for Vole buffer cleanup.

When a PV loses all Hare subscribers (deactivation), the GraceManager starts a
timer. If no new subscriber arrives before the timer fires, Vole unsubscribes
from the PV and drops its buffer. If a subscriber arrives (activation), the
pending timer is cancelled and the buffer stays warm.

A pinned PV (held by a live table eval) is never torn down: an expiry that fires
while the PV is pinned is skipped and remembered, and the last unpin re-arms that
same expiry. Unpin never arms grace on its own -- grace stays driven only by Hare
deactivation, so a REST-only ring keeps today's lifetime.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Protocol
from collections.abc import Callable

from canopy.epics.pv_protocol import strip_protocol_prefix
from canopy.vole.config import VoleConfig

logger = logging.getLogger(__name__)


class IngestProtocol(Protocol):
    @property
    def subscribed_pvs(self) -> set[str]: ...

    async def unsubscribe(self, pvs: list[str]) -> None: ...


class BufferProtocol(Protocol):
    async def drop(self, pv: str) -> None: ...


class GraceManager:
    def __init__(
        self,
        ingest: IngestProtocol,
        buffer: BufferProtocol,
        config: VoleConfig,
        *,
        is_pinned: Callable[[str], bool] | None = None,
    ) -> None:
        self._ingest = ingest
        self._buffer = buffer
        self._grace_seconds = config.buffer_grace_seconds
        self._is_pinned = is_pinned or (lambda pv: False)
        self._pending: dict[str, asyncio.Task] = {}
        # Deactivated PVs whose expiry fired while pinned, re-armed by the last unpin.
        # Keyed by the stripped name (pins are stripped; Hare's deactivation name may
        # carry ca://), valued by the name the deactivation used, for the re-arm.
        self._skipped: dict[str, str] = {}

    async def on_pv_deactivated(self, pvs: list[str]) -> None:
        # The property copies, so read it once rather than per PV.
        subscribed = self._ingest.subscribed_pvs
        for pv in pvs:
            if pv in subscribed and pv not in self._pending:
                self._pending[pv] = asyncio.create_task(self._expire(pv))

    async def on_pv_activated(self, pvs: list[str]) -> None:
        for pv in pvs:
            self._skipped.pop(strip_protocol_prefix(pv), None)
            task = self._pending.pop(pv, None)
            if task:
                task.cancel()

    async def on_pvs_unpinned(self, pvs: list[str]) -> None:
        """The last pin on these PVs is gone: re-arm the expiries skipped while pinned."""
        to_rearm = [orig for pv in pvs if (orig := self._skipped.pop(strip_protocol_prefix(pv), None)) is not None]
        if to_rearm:
            await self.on_pv_deactivated(to_rearm)

    async def _expire(self, pv: str) -> None:
        """Wait out the grace period, then unsubscribe ingest and drop the ring.

        Stays in ``_pending`` until the ``finally`` so a reactivation during the
        teardown awaits can still cancel. Cancelling between the unsubscribe and
        the drop leaves the ring alive but detached; the next subscribe
        re-attaches ingest, so it gets a gap rather than being orphaned.
        """
        try:
            await asyncio.sleep(self._grace_seconds)
            if self._is_pinned(pv):
                self._skipped[strip_protocol_prefix(pv)] = pv
                logger.debug("Grace period expired for pinned %s; teardown deferred to its last unpin", pv)
                return
            await self._ingest.unsubscribe([pv])
            await self._buffer.drop(pv)
            logger.debug("Grace period expired; unsubscribed and dropped buffer for %s", pv)
        except asyncio.CancelledError:
            raise
        except Exception:
            # Nothing retrieves this task, so an unlogged error is invisible.
            logger.exception("Grace period teardown failed for %s", pv)
        finally:
            self._pending.pop(pv, None)

    async def shutdown(self) -> None:
        tasks = list(self._pending.values())
        for task in tasks:
            task.cancel()
        self._pending.clear()
        self._skipped.clear()
        await asyncio.gather(*tasks, return_exceptions=True)
