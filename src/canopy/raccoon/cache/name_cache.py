"""Bounded-LRU cache for per-PV existence answers.

Entries carry a boolean (the answer) and an expiry timestamp. Positive and
negative entries use different default TTLs because a PV that exists in the
directory is far more stable than a transient miss.

The cache is per-replica and lives only in process memory. Cold-start strategy
is none: the first request after a restart pays the upstream latency.
"""

import time
from collections import OrderedDict
from dataclasses import dataclass
from collections.abc import Callable


@dataclass(frozen=True, slots=True)
class ExistsEntry:
    known: bool
    expires_at: float


class PVNameCache:
    """LRU cache keyed by PV name; values carry their own expiry.

    Eviction is least-recently-accessed: every hit reorders the entry to the
    most-recent end. Insertion past the size ceiling drops the LRU end.
    """

    def __init__(
        self,
        max_entries: int,
        positive_ttl_s: float,
        negative_ttl_s: float,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._max = max_entries
        self._positive_ttl = positive_ttl_s
        self._negative_ttl = negative_ttl_s
        self._clock = clock
        self._data: OrderedDict[str, ExistsEntry] = OrderedDict()
        self.hits: int = 0
        self.misses: int = 0
        self.evictions: int = 0

    def get(self, pv: str) -> bool | None:
        """Return the cached answer, or None on miss/expired."""
        entry = self._data.get(pv)
        if entry is None:
            self.misses += 1
            return None
        if entry.expires_at <= self._clock():
            del self._data[pv]
            self.misses += 1
            return None
        self._data.move_to_end(pv)
        self.hits += 1
        return entry.known

    def set(self, pv: str, known: bool) -> None:
        ttl = self._positive_ttl if known else self._negative_ttl
        self._data[pv] = ExistsEntry(known=known, expires_at=self._clock() + ttl)
        self._data.move_to_end(pv)
        while len(self._data) > self._max:
            self._data.popitem(last=False)
            self.evictions += 1

    def size(self) -> int:
        return len(self._data)

    def clear(self) -> None:
        self._data.clear()
