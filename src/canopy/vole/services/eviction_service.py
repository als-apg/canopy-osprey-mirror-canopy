from __future__ import annotations

import logging

from canopy.vole.config import VoleConfig
from canopy.vole.buffer.store import PVStore

logger = logging.getLogger(__name__)


class EvictionService:
    """Memory pressure detection and LRU eviction."""

    def __init__(self, store: PVStore, config: VoleConfig) -> None:
        self._store = store
        self._config = config

    def under_pressure(self) -> bool:
        return self._store.total_bytes > self._config.global_bytes_max

    def derived_ring_over_limit(self) -> bool:
        limit = int(self._config.global_bytes_max * self._config.derived_ring_bytes_max_pct)
        derived_bytes = sum(
            ring.byte_estimate for name, ring in self._store.lru_candidates() if name.startswith("fox:")
        )
        return derived_bytes > limit

    async def evict_lru(self, *, target_bytes: int | None = None) -> list[str]:
        """Drop whole rings by LRU until under ceiling. Returns PV names dropped."""
        ceiling = target_bytes if target_bytes is not None else self._config.global_bytes_max
        dropped: list[str] = []

        while self._store.total_bytes > ceiling:
            candidates = self._store.lru_candidates()
            if not candidates:
                break
            pv, ring = candidates[0]
            self._store.drop(pv)
            dropped.append(pv)
            logger.info("Evicted ring %r (%d bytes)", pv, ring.byte_estimate)

        return dropped
