"""Grace period manager for Vole buffer cleanup.

When a PV loses all Hare subscribers (deactivation), the GraceManager starts a
timer. If no new subscriber arrives before the timer fires, Vole unsubscribes
from the PV and drops its buffer. If a subscriber arrives (activation), the
pending timer is cancelled and the buffer stays warm.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Protocol, runtime_checkable

from canopy.vole.config import VoleConfig

logger = logging.getLogger(__name__)


@runtime_checkable
class IngestProtocol(Protocol):
    _subscribed_pvs: set[str]

    async def unsubscribe(self, pvs: list[str]) -> None: ...


class GraceManager:
    def __init__(self, ingest: IngestProtocol, config: VoleConfig) -> None:
        self._ingest = ingest
        self._grace_seconds = config.buffer_grace_seconds
        self._pending: dict[str, asyncio.Task] = {}

    async def on_pv_deactivated(self, pvs: list[str]) -> None:
        for pv in pvs:
            if pv in self._ingest._subscribed_pvs and pv not in self._pending:
                self._pending[pv] = asyncio.create_task(self._expire(pv))

    async def on_pv_activated(self, pvs: list[str]) -> None:
        for pv in pvs:
            task = self._pending.pop(pv, None)
            if task:
                task.cancel()

    async def _expire(self, pv: str) -> None:
        await asyncio.sleep(self._grace_seconds)
        self._pending.pop(pv, None)
        await self._ingest.unsubscribe([pv])
        logger.debug("Grace period expired; unsubscribed Vole from %s", pv)

    async def shutdown(self) -> None:
        for task in self._pending.values():
            task.cancel()
        self._pending.clear()
