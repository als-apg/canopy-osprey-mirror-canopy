from __future__ import annotations

import asyncio

from canopy.vole.config import VoleConfig
from canopy.vole.buffer.ring import PerPVRing


class PVStore:
    """Registry of per-PV rings with lifecycle management."""

    def __init__(self, config: VoleConfig) -> None:
        self._config = config
        self._rings: dict[str, PerPVRing] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def get(self, pv: str) -> PerPVRing | None:
        return self._rings.get(pv)

    def get_or_create(
        self,
        pv: str,
        *,
        max_age_ns: int | None = None,
        max_points: int | None = None,
    ) -> PerPVRing:
        if pv not in self._rings:
            self._rings[pv] = PerPVRing(
                max_age_ns=max_age_ns or self._config.max_age_ns,
                max_points=max_points or self._config.max_points,
            )
            self._locks[pv] = asyncio.Lock()
        return self._rings[pv]

    def drop(self, pv: str) -> bool:
        if pv in self._rings:
            del self._rings[pv]
            del self._locks[pv]
            return True
        return False

    def contains(self, pv: str) -> bool:
        return pv in self._rings

    @property
    def pv_names(self) -> list[str]:
        return list(self._rings.keys())

    @property
    def total_bytes(self) -> int:
        return sum(r.byte_estimate for r in self._rings.values())

    @property
    def total_points(self) -> int:
        return sum(r.count for r in self._rings.values())

    @property
    def ring_count(self) -> int:
        return len(self._rings)

    def lock_for(self, pv: str) -> asyncio.Lock:
        if pv not in self._locks:
            self._locks[pv] = asyncio.Lock()
        return self._locks[pv]

    def lru_candidates(self) -> list[tuple[str, PerPVRing]]:
        """Return rings sorted by last_access_ns ascending (LRU first)."""
        candidates = list(self._rings.items())
        candidates.sort(key=lambda pair: pair[1].last_access_ns)
        return candidates
