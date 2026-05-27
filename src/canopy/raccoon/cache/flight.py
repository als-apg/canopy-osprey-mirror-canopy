"""Single-flight de-duplication for concurrent identical lookups.

If 100 callers ask for the same uncached `(kind, pattern, ...)` at once, only
one upstream call goes out. The rest await the same future. Once it resolves,
the result fans out to every awaiter and the slot is cleared so the next call
that misses the cache will start a new flight.

The internal lock is held only across dict mutation, never across the upstream
call itself.
"""

import asyncio
from typing import TypeVar
from collections.abc import Hashable, Awaitable, Callable

T = TypeVar("T")


class SingleFlight:
    def __init__(self) -> None:
        self._inflight: dict[Hashable, asyncio.Future] = {}
        self._lock = asyncio.Lock()

    def in_flight_count(self) -> int:
        return len(self._inflight)

    async def run(self, key: Hashable, factory: Callable[[], Awaitable[T]]) -> T:
        """Execute `factory()` once per `key` for any concurrent set of callers.

        Subsequent callers with the same key, while the first call is still
        running, await the same future. When the future settles (success or
        exception), it propagates to every awaiter and the key is freed.
        """
        async with self._lock:
            existing = self._inflight.get(key)
            if existing is not None:
                fut: asyncio.Future = existing
                owns = False
            else:
                fut = asyncio.get_event_loop().create_future()
                self._inflight[key] = fut
                owns = True

        if not owns:
            return await fut

        try:
            result = await factory()
        except BaseException as exc:
            fut.set_exception(exc)
            async with self._lock:
                self._inflight.pop(key, None)
            raise
        else:
            fut.set_result(result)
            async with self._lock:
                self._inflight.pop(key, None)
            return result
