"""Coordinates store/alignment/eviction; the implementation behind the gateway."""

from __future__ import annotations

import logging
from typing import Literal

from canopy.core.clock import Clock
from canopy.vole.config import VoleConfig
from canopy.vole.errors import PVNotBuffered, BufferAtCapacity
from canopy.core.decimation import uniform_time_decimate
from canopy.contracts.sample import Sample
from canopy.vole.buffer.ring import StoredSample
from canopy.vole.buffer.store import PVStore
from canopy.vole.schemas.buffer import PerPVInfoDTO, VoleStatsDTO
from canopy.vole.services.eviction_service import EvictionService
from canopy.vole.services.alignment_service import AlignedResult, AlignmentService

logger = logging.getLogger(__name__)


class BufferService:
    """Orchestrates store, alignment, and eviction for the Vole buffer."""

    def __init__(
        self,
        store: PVStore,
        alignment: AlignmentService,
        eviction: EvictionService,
        clock: Clock,
        config: VoleConfig,
    ) -> None:
        self._store = store
        self._alignment = alignment
        self._eviction = eviction
        self._clock = clock
        self._config = config

    # --- writes ---------------------------------------------------------- #

    async def append(self, pv: str, sample: Sample) -> None:
        """Append a live sample to the PV's ring."""
        if self._eviction.under_pressure():
            await self._eviction.evict_lru()
            if self._eviction.under_pressure():
                raise BufferAtCapacity(f"Global memory ceiling exceeded; cannot append to {pv}")

        ring = self._store.get_or_create(pv)
        stored = self._sample_to_stored(sample, source_override="live")
        now = self._clock.now_ns()

        async with self._store.lock_for(pv):
            ring.append(stored, now)

    async def seed(
        self,
        pv: str,
        samples: list[Sample],
        *,
        max_age_ns: int | None = None,
        max_points: int | None = None,
    ) -> int:
        """Bulk insert historical samples; returns count actually stored."""
        if not samples:
            return 0

        ring = self._store.get_or_create(pv, max_age_ns=max_age_ns, max_points=max_points)
        if max_age_ns is not None or max_points is not None:
            ring.update_ceilings(max_age_ns=max_age_ns, max_points=max_points)

        stored_samples = sorted(
            [self._sample_to_stored(s, source_override="archive") for s in samples],
            key=lambda s: s.ts_ns,
        )
        now = self._clock.now_ns()

        async with self._store.lock_for(pv):
            return ring.seed(stored_samples, now)

    async def drop(self, pv: str) -> None:
        """Remove a PV's ring entirely. Idempotent."""
        self._store.drop(pv)

    # --- reads ----------------------------------------------------------- #

    async def read_window(
        self,
        pv: str,
        t0_ns: int,
        t1_ns: int,
        *,
        align: str = "last",
        max_points: int | None = None,
    ) -> list[Sample]:
        """Single-PV window read with optional decimation."""
        ring = self._store.get(pv)
        if ring is None:
            raise PVNotBuffered(f"PV not buffered: {pv}", pvs=[pv])

        t1_ns = self._resolve_t1(t1_ns)
        now = self._clock.now_ns()

        async with self._store.lock_for(pv):
            ring.touch(now)
            samples = ring.slice(t0_ns, t1_ns)

            result: list[Sample] = []
            if align == "last" and samples:
                pre = ring.sample_before(t0_ns)
                if pre is not None and (not samples or samples[0].ts_ns > t0_ns):
                    result.append(
                        Sample(
                            ts_ns=t0_ns,
                            value=pre.value,
                            severity=pre.severity,
                            source=pre.source,
                        )
                    )
            result.extend(self._stored_to_sample(s) for s in samples)

        if max_points is not None and len(result) > max_points:
            result = uniform_time_decimate(result, max_points)

        return result

    async def read_aligned(
        self,
        pvs: list[str],
        t0_ns: int,
        t1_ns: int,
        *,
        align: str = "last",
        index: str = "union",
        grid_dt_ns: int | None = None,
    ) -> AlignedResult:
        """Multi-PV aligned read."""
        missing = [pv for pv in pvs if not self._store.contains(pv)]
        if missing:
            raise PVNotBuffered(f"PVs not buffered: {missing}", pvs=missing)

        t1_ns = self._resolve_t1(t1_ns)
        now = self._clock.now_ns()

        raw_slices: dict[str, list[StoredSample]] = {}
        pre_window_samples: dict[str, StoredSample | None] = {}

        # Acquire locks in sorted PV-name order to prevent deadlocks
        for pv in sorted(pvs):
            async with self._store.lock_for(pv):
                ring = self._store.get(pv)
                assert ring is not None
                ring.touch(now)
                raw_slices[pv] = ring.slice(t0_ns, t1_ns)
                pre_window_samples[pv] = ring.sample_before(t0_ns)

        return self._alignment.align(
            raw_slices,
            t0_ns,
            t1_ns,
            align=align,  # type: ignore[arg-type]
            index=index,
            grid_dt_ns=grid_dt_ns,
            pre_window_samples=pre_window_samples,
        )

    async def read_latest(self, pv: str) -> Sample | None:
        """Return the most recent sample for a PV, or None."""
        ring = self._store.get(pv)
        if ring is None:
            return None

        now = self._clock.now_ns()
        async with self._store.lock_for(pv):
            ring.touch(now)
            latest = ring.latest

        if latest is None:
            return None
        return self._stored_to_sample(latest)

    # --- introspection --------------------------------------------------- #

    async def stats(self) -> VoleStatsDTO:
        rings_info: list[PerPVInfoDTO] = []
        for pv in self._store.pv_names:
            ring = self._store.get(pv)
            if ring is None:
                continue
            rings_info.append(
                PerPVInfoDTO(
                    pv=pv,
                    point_count=ring.count,
                    byte_estimate=ring.byte_estimate,
                    oldest_ts_ns=ring.earliest.ts_ns if ring.earliest else None,
                    newest_ts_ns=ring.latest.ts_ns if ring.latest else None,
                    last_access_ns=ring.last_access_ns,
                )
            )

        return VoleStatsDTO(
            ring_count=self._store.ring_count,
            total_points=self._store.total_points,
            total_bytes=self._store.total_bytes,
            global_bytes_max=self._config.global_bytes_max,
            pressure=self._eviction.under_pressure(),
            rings=rings_info,
        )

    # --- internal helpers ------------------------------------------------ #

    def _sample_to_stored(self, sample: Sample, *, source_override: str | None = None) -> StoredSample:
        source: Literal["live", "archive"] = (
            source_override if source_override in ("live", "archive") else sample.source
        )  # type: ignore[assignment]
        return StoredSample(
            ts_ns=sample.ts_ns,
            value=sample.value,
            severity=sample.severity,
            source=source,
        )

    def _stored_to_sample(self, stored: StoredSample) -> Sample:
        return Sample(
            ts_ns=stored.ts_ns,
            value=stored.value,
            severity=stored.severity,
            source=stored.source,
        )

    def _resolve_t1(self, t1_ns: int) -> int:
        if t1_ns == 0:
            return self._clock.now_ns()
        return t1_ns
