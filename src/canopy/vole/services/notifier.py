"""Change-notification pub/sub for Vole ring buffers.

When a ring receives new data via append(), the notifier fires registered
callbacks so downstream consumers (Fox) can react without polling.
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from collections.abc import Callable, Awaitable

logger = logging.getLogger(__name__)

ChangeCallback = Callable[[str], Awaitable[None]]


class ChangeNotifier:
    """Callback-based notification registry for PV data changes."""

    def __init__(self) -> None:
        self._listeners: dict[str, list[ChangeCallback]] = defaultdict(list)
        self._tasks: set[asyncio.Task] = set()

    def subscribe(self, pv: str, callback: ChangeCallback) -> Callable[[], None]:
        """Register a callback for PV changes. Returns an unsubscribe function."""
        self._listeners[pv].append(callback)

        def _unsub() -> None:
            try:
                self._listeners[pv].remove(callback)
            except ValueError:
                pass
            if not self._listeners[pv]:
                del self._listeners[pv]

        return _unsub

    def notify(self, pv: str) -> None:
        """Fire-and-forget: schedule all callbacks for this PV as asyncio tasks."""
        callbacks = self._listeners.get(pv)
        if not callbacks:
            return
        for cb in list(callbacks):
            task = asyncio.create_task(self._safe_call(cb, pv))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    @staticmethod
    async def _safe_call(cb: ChangeCallback, pv: str) -> None:
        try:
            await cb(pv)
        except Exception:
            logger.warning("Notifier callback error for PV %s", pv, exc_info=True)
