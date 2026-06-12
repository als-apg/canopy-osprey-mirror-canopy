"""Bounded-LRU cache for pattern -> name-list answers.

Keyed on `CacheKey` (kind, pattern, tag, sort_by, element_type) so that two
queries that differ in any knob occupy distinct entries. Entries expire after
`pattern_ttl_s`; the directory service changes rarely but new PVs do appear
after IOC restarts, so the TTL is shorter than the per-PV existence TTL.
"""

import time
from collections import OrderedDict
from dataclasses import dataclass
from collections.abc import Callable

from canopy.raccoon.cache.keys import CacheKey


@dataclass(frozen=True, slots=True)
class PatternEntry:
    names: tuple[str, ...]
    expires_at: float


class PatternCache:
    def __init__(
        self,
        max_entries: int,
        ttl_s: float,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._max = max_entries
        self._ttl = ttl_s
        self._clock = clock
        self._data: OrderedDict[CacheKey, PatternEntry] = OrderedDict()
        self.hits: int = 0
        self.misses: int = 0
        self.evictions: int = 0

    def get(self, key: CacheKey) -> list[str] | None:
        entry = self._data.get(key)
        if entry is None:
            self.misses += 1
            return None
        if entry.expires_at <= self._clock():
            del self._data[key]
            self.misses += 1
            return None
        self._data.move_to_end(key)
        self.hits += 1
        return list(entry.names)

    def set(self, key: CacheKey, names: list[str]) -> None:
        self._data[key] = PatternEntry(names=tuple(names), expires_at=self._clock() + self._ttl)
        self._data.move_to_end(key)
        while len(self._data) > self._max:
            self._data.popitem(last=False)
            self.evictions += 1

    def size(self) -> int:
        return len(self._data)

    def clear(self) -> None:
        self._data.clear()
