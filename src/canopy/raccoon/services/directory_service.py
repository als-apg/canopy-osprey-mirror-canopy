"""Directory orchestrator: caches + single-flight + adapter coordination.

The gateway functions in `canopy.raccoon.gateway` are one-line wrappers around
this class. Per `Module-Architecture-Raccoon.md` §10, the cache is the data
tier: nothing in `api/` reaches past the service to the adapter.
"""

from __future__ import annotations

import time
import asyncio
import logging

from canopy.raccoon.config import RaccoonConfig
from canopy.raccoon.cache.flight import SingleFlight
from canopy.raccoon.cache.keys import CacheKey, NameKind
from canopy.raccoon.cache.name_cache import PVNameCache
from canopy.raccoon.cache.pattern_cache import PatternCache
from canopy.raccoon.adapters.registry import DirectoryRegistry
from canopy.raccoon.schemas.directory import (
    CacheStatsDTO,
    RaccoonStatsDTO,
    ValidateResultDTO,
)
from canopy.raccoon.adapters.base import (
    AdapterHealth,
    InvalidQuery,
    DirectoryTimeout,
    DirectoryUnavailable,
)

logger = logging.getLogger(__name__)

# Characters that, in any input to `exists`/`validate`, indicate the caller
# meant a pattern. Use `list_pvs` for those paths.
_PATTERN_METACHARS = frozenset("%*?(|[")


def _has_pattern_metachars(s: str) -> bool:
    return any(c in _PATTERN_METACHARS for c in s)


class DirectoryService:
    """Coordinates the adapter, the two caches, and the single-flight tracker.

    Constructed once per `canopy-api` process by `raccoon_lifespan`. Tests
    construct their own with a `FakeDirectoryAdapter` and substitute it via
    `canopy.raccoon.gateway.bind`.
    """

    def __init__(
        self,
        registry: DirectoryRegistry,
        pattern_cache: PatternCache,
        name_cache: PVNameCache,
        flight: SingleFlight,
        config: RaccoonConfig,
    ) -> None:
        self._registry = registry
        self._pattern_cache = pattern_cache
        self._name_cache = name_cache
        self._flight = flight
        self._config = config
        self._timeout_s = config.directory.timeout_s if config.directory else 5.0
        self._batch_concurrency = config.directory.batch_concurrency if config.directory else 64

    # ------------------------------------------------------------------ #
    # Public API used by the gateway facade.
    # ------------------------------------------------------------------ #

    async def list(
        self,
        kind: NameKind,
        pattern: str,
        *,
        tag: str | None = None,
        sort_by: str | None = None,
        element_type: str | None = None,
    ) -> list[str]:
        key = CacheKey(kind=kind, pattern=pattern, tag=tag, sort_by=sort_by, element_type=element_type)

        cached = self._pattern_cache.get(key)
        if cached is not None:
            return cached

        async def factory() -> list[str]:
            # Re-check after acquiring the flight slot; another caller may have
            # populated the cache while we were waiting for the lock.
            cached_again = self._pattern_cache.get(key)
            if cached_again is not None:
                return cached_again

            adapter = self._registry.require()
            try:
                if kind is NameKind.PV:
                    coro = adapter.list_pvs(pattern, tag=tag, sort_by=sort_by, element_type=element_type)
                elif kind is NameKind.DEVICE:
                    coro = adapter.list_devices(pattern, tag=tag, sort_by=sort_by, element_type=element_type)
                else:
                    coro = adapter.list_elements(pattern, tag=tag, sort_by=sort_by, element_type=element_type)
                names = await asyncio.wait_for(coro, timeout=self._timeout_s)
            except TimeoutError as exc:
                raise DirectoryTimeout(f"Directory call exceeded {self._timeout_s}s") from exc

            self._pattern_cache.set(key, names)
            if kind is NameKind.PV:
                # Opportunistic warm: every returned literal name is known to
                # exist, so future `exists()` calls hit the cache for free.
                # Patterns that return N names tell us nothing about which
                # *other* names are missing, so we never warm negatives here.
                for n in names:
                    self._name_cache.set(n, True)
            return names

        return await self._flight.run(key, factory)

    async def exists(self, pv: str) -> bool:
        if _has_pattern_metachars(pv):
            raise InvalidQuery(f"PV name contains pattern metacharacters: {pv!r}")

        cached = self._name_cache.get(pv)
        if cached is not None:
            return cached

        names = await self.list(NameKind.PV, pv)
        known = pv in names
        self._name_cache.set(pv, known)
        return known

    async def validate(self, pvs: list[str]) -> ValidateResultDTO:
        for pv in pvs:
            if _has_pattern_metachars(pv):
                raise InvalidQuery(f"PV name contains pattern metacharacters: {pv!r}")

        # Partition by cache. Hold the original input order so callers can
        # match by index against the response lists.
        cached_results: dict[str, bool] = {}
        to_query: list[str] = []
        for pv in pvs:
            hit = self._name_cache.get(pv)
            if hit is None:
                if pv not in cached_results:
                    to_query.append(pv)
            else:
                cached_results[pv] = hit

        # Bound concurrency so a 5k-name validate doesn't fan out 5k upstream
        # calls. Single-flight still coalesces duplicate work.
        if to_query:
            sem = asyncio.Semaphore(self._batch_concurrency)

            async def _one(pv: str) -> tuple[str, bool]:
                async with sem:
                    return pv, await self.exists(pv)

            results = await asyncio.gather(*(_one(pv) for pv in to_query))
            for pv, known in results:
                cached_results[pv] = known

        known_list = [pv for pv in pvs if cached_results.get(pv, False)]
        unknown_list = [pv for pv in pvs if not cached_results.get(pv, False)]
        return ValidateResultDTO(known=known_list, unknown=unknown_list)

    async def stats(self) -> RaccoonStatsDTO:
        adapter = self._registry.adapter
        if adapter is None:
            health = AdapterHealth(
                healthy=False,
                last_error="No directory adapter configured",
                last_check_ts_ns=time.time_ns(),
            )
            adapter_name = ""
            adapter_type = ""
        else:
            try:
                health = await asyncio.wait_for(adapter.health(), timeout=self._timeout_s)
            except (TimeoutError, DirectoryUnavailable) as exc:
                health = AdapterHealth(
                    healthy=False,
                    last_error=str(exc),
                    last_check_ts_ns=time.time_ns(),
                )
            adapter_name = adapter.name
            adapter_type = getattr(adapter, "type_name", "")

        return RaccoonStatsDTO(
            adapter_name=adapter_name,
            adapter_type=adapter_type,
            adapter_health=health,
            pattern_cache=CacheStatsDTO(
                size=self._pattern_cache.size(),
                hits=self._pattern_cache.hits,
                misses=self._pattern_cache.misses,
                evictions=self._pattern_cache.evictions,
            ),
            name_cache=CacheStatsDTO(
                size=self._name_cache.size(),
                hits=self._name_cache.hits,
                misses=self._name_cache.misses,
                evictions=self._name_cache.evictions,
            ),
            in_flight=self._flight.in_flight_count(),
        )
