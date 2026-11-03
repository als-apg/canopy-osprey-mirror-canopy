from __future__ import annotations

import logging
from collections.abc import Callable, Awaitable

from canopy.vole.config import VoleConfig
from canopy.vole.buffer.store import PVStore

logger = logging.getLogger(__name__)

OnEvictFn = Callable[[list[str]], Awaitable[None]]


class EvictionService:
    """Memory pressure detection and LRU eviction.

    Unpinned rings are dropped whole, LRU first. When only pinned rings remain
    over the ceiling, the largest ones are shrunk to ``settle_depth`` instead of
    dropped, so the ingest subscription that is the reason for the pin survives
    and Vole stays bounded. A shrink forgets the ring's provisioned history.
    """

    def __init__(self, store: PVStore, config: VoleConfig, on_evict: OnEvictFn | None = None) -> None:
        self._store = store
        self._config = config
        self._on_evict = on_evict
        # (store generation, total bytes) of the last ceiling pass that found nothing
        # to drop or shrink. Until a ring is added, grown or unpinned, or the bytes
        # grow past that mark, another pass cannot relieve anything, so it is skipped
        # rather than re-sorting every ring on every live append.
        self._stuck: tuple[int, int] | None = None

    def under_pressure(self) -> bool:
        return self._store.total_bytes > self._config.global_bytes_max

    async def evict_lru(self, *, target_bytes: int | None = None) -> list[str]:
        """Drop unpinned rings by LRU, then shrink pinned ones, until under ceiling.

        Returns the PV names dropped. Stops when nothing is left to drop or shrink.
        """
        ceiling = target_bytes if target_bytes is not None else self._config.global_bytes_max
        settle_depth = self._config.settle_depth
        dropped: list[str] = []

        if target_bytes is None and self._stuck is not None:
            generation, stuck_bytes = self._stuck
            if self._store.generation == generation and self._store.total_bytes <= stuck_bytes:
                return dropped
        self._stuck = None

        while self._store.total_bytes > ceiling:
            candidates = self._store.lru_candidates()
            if candidates:
                pv, ring = candidates[0]
                self._store.drop(pv)
                dropped.append(pv)
                logger.info("Evicted ring %r (%d bytes)", pv, ring.byte_estimate)
                continue
            shrinkable = [(pv, ring) for pv, ring in self._store.pinned_candidates() if ring.count > settle_depth]
            if not shrinkable:
                if target_bytes is None:
                    self._stuck = (self._store.generation, self._store.total_bytes)
                break
            pv, ring = shrinkable[0]
            trimmed = ring.shrink_to(settle_depth)
            logger.info("Shrank pinned ring %r to %d samples (%d trimmed)", pv, settle_depth, trimmed)

        if dropped and self._on_evict is not None:
            await self._on_evict(dropped)

        return dropped
