"""Grace period manager for Vole buffer cleanup.

When a PV loses all Hare subscribers (deactivation), the GraceManager starts a
timer. If no new subscriber arrives before the timer fires, Vole unsubscribes
from the PV and drops its buffer. If a subscriber arrives (activation), the
pending timer is cancelled and the buffer stays warm.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Protocol

from canopy.vole.config import VoleConfig

logger = logging.getLogger(__name__)


class IngestProtocol(Protocol):
    @property
    def subscribed_pvs(self) -> set[str]: ...

    async def unsubscribe(self, pvs: list[str]) -> None: ...


class BufferProtocol(Protocol):
    async def drop(self, pv: str) -> None: ...


class GraceManager:
    def __init__(self, ingest: IngestProtocol, buffer: BufferProtocol, config: VoleConfig) -> None:
        self._ingest = ingest
        self._buffer = buffer
        self._grace_seconds = config.buffer_grace_seconds
        self._pending: dict[str, asyncio.Task] = {}

    async def on_pv_deactivated(self, pvs: list[str]) -> None:
        # The property copies, so read it once rather than per PV.
        subscribed = self._ingest.subscribed_pvs
        for pv in pvs:
            if pv in subscribed and pv not in self._pending:
                self._pending[pv] = asyncio.create_task(self._expire(pv))

    async def on_pv_activated(self, pvs: list[str]) -> None:
        for pv in pvs:
            task = self._pending.pop(pv, None)
            if task:
                task.cancel()

    async def _expire(self, pv: str) -> None:
        """Wait out the grace period, then unsubscribe ingest and drop the ring.

        Stays in ``_pending`` until the ``finally`` so a reactivation during the
        teardown awaits can still cancel. Cancelling between the unsubscribe and
        the drop leaves the ring alive but detached; the next subscribe
        re-attaches ingest, so it gets a gap rather than being orphaned.
        """
        try:
            await asyncio.sleep(self._grace_seconds)
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
        await asyncio.gather(*tasks, return_exceptions=True)
