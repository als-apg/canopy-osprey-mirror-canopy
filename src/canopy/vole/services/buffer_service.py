"""Coordinates store/alignment/eviction; the implementation behind the gateway."""

from __future__ import annotations

import logging
from typing import Literal

from canopy.core.clock import Clock
from canopy.vole.config import VoleConfig
from canopy.vole.errors import PVNotBuffered, BufferAtCapacity
from canopy.core.decimation import uniform_time_decimate
from canopy.contracts.sample import Sample
from canopy.vole.buffer.ring import Outage, StoredSample
from canopy.vole.buffer.store import PVStore
from canopy.vole.schemas.buffer import PerPVInfoDTO, VoleStatsDTO
from canopy.vole.services.notifier import ChangeNotifier
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
        notifier: ChangeNotifier | None = None,
    ) -> None:
        self._store = store
        self._alignment = alignment
        self._eviction = eviction
        self._clock = clock
        self._config = config
        self._notifier = notifier
        self._retry_after: dict[str, int] = {}

    @property
    def max_points(self) -> int:
        return self._config.max_points

    @property
    def clock(self) -> Clock:
        return self._clock

    @property
    def provision_retry_ns(self) -> int:
        return int(self._config.provision_retry_s * 1_000_000_000)

    # --- writes ---------------------------------------------------------- #

    async def append(self, pv: str, sample: Sample, *, recv_ns: int = 0, after_connect: bool = False) -> None:
        """Append a live sample to the PV's ring.

        Under unrelievable memory pressure an append to an unpinned ring is
        refused; a pinned ring (bounded by its own ceiling, shrunk by eviction)
        keeps ingesting, since its subscription is the reason it is pinned.
        """
        if self._eviction.under_pressure():
            await self._eviction.evict_lru()
            if self._eviction.under_pressure() and not self._store.is_pinned(pv):
                raise BufferAtCapacity(f"Global memory ceiling exceeded; cannot append to {pv}")

        ring = self._store.get_or_create(pv)
        stored = self._sample_to_stored(sample, source_override="live", recv_ns=recv_ns, after_connect=after_connect)
        now = self._clock.now_ns()

        async with self._store.lock_for(pv):
            ring.append(stored, now)

        if self._notifier is not None:
            self._notifier.notify(pv)

    async def seed(
        self,
        pv: str,
        samples: list[Sample],
        *,
        max_points: int | None = None,
    ) -> int:
        """Bulk insert historical samples; returns count actually stored."""
        ring = self._store.get_or_create(pv, max_points=max_points)
        if not samples:
            return 0
        if max_points is not None:
            ring.update_ceilings(max_points=max_points)

        stored_samples = sorted(
            [self._sample_to_stored(s, source_override="archive") for s in samples],
            key=lambda s: s.ts_ns,
        )
        now = self._clock.now_ns()

        async with self._store.lock_for(pv):
            count = ring.seed(stored_samples, now)

        if self._eviction.under_pressure():
            await self._eviction.evict_lru()

        return count

    async def drop(self, pv: str) -> None:
        """Remove a PV's ring entirely. Idempotent."""
        self._store.drop(pv)

    # --- pins, capacity, provisioning ------------------------------------ #

    def pin(self, pvs: list[str], holder: str) -> None:
        self._store.pin(pvs, holder)

    def unpin(self, holder: str) -> list[str]:
        return self._store.unpin(holder)

    def is_pinned(self, pv: str) -> bool:
        return self._store.is_pinned(pv)

    def ensure_capacity(self, pv: str, max_points: int | None = None) -> None:
        """Grow ``pv``'s ring to at least ``max_points`` (default ``settle_depth``); never shrinks."""
        self._store.ensure_capacity(pv, max_points or self._config.settle_depth)

    def touch(self, pv: str, now_ns: int) -> None:
        ring = self._store.get(pv)
        if ring is not None:
            ring.touch(now_ns)

    def ring_max_points(self, pv: str) -> int | None:
        ring = self._store.get(pv)
        return ring.max_points if ring is not None else None

    def ring_count(self, pv: str) -> int:
        ring = self._store.get(pv)
        return ring.count if ring is not None else 0

    def is_provisioned(self, pv: str) -> bool:
        ring = self._store.get(pv)
        return ring is not None and ring.provisioned

    def mark_provisioned(self, pv: str) -> None:
        self._store.get_or_create(pv).provisioned = True

    def provision_retry_after_ns(self, pv: str) -> int:
        ring = self._store.get(pv)
        side = self._retry_after.get(pv, 0)
        if ring is None:
            return side
        if side:
            # A deferral recorded before the ring existed moves onto the ring.
            del self._retry_after[pv]
            ring.provision_retry_after_ns = max(ring.provision_retry_after_ns, side)
        return ring.provision_retry_after_ns

    def defer_provision(self, pv: str, until_ns: int) -> None:
        """Record that a failed provision of ``pv`` may be retried after ``until_ns``.

        Kept on the ring when there is one, else in a side table (a refused PV has
        no ring yet) so a PV the archiver refuses costs one Mole call per period.
        """
        ring = self._store.get(pv)
        if ring is not None:
            ring.provision_retry_after_ns = until_ns
        else:
            self._retry_after[pv] = until_ns

    # --- sample-record port (synchronous under the per-PV state) --------- #

    def last_at_or_before(self, pv: str, ts_ns: int) -> StoredSample | None:
        ring = self._store.get(pv)
        return ring.last_at_or_before(ts_ns) if ring is not None else None

    def read_latest_sync(self, pv: str) -> StoredSample | None:
        ring = self._store.get(pv)
        return ring.latest if ring is not None else None

    def in_outage(self, pv: str, ts_ns: int) -> bool:
        ring = self._store.get(pv)
        return ring.in_outage(ts_ns) if ring is not None else False

    def outages(self, pv: str) -> list[Outage]:
        ring = self._store.get(pv)
        return ring.outages if ring is not None else []

    def samples_since(self, pv: str, recv_after_ns: int) -> list[StoredSample]:
        ring = self._store.get(pv)
        return ring.samples_since(recv_after_ns) if ring is not None else []

    def ring_slice(self, pv: str, ts_from_ns: int) -> tuple[list[StoredSample], list[Outage]]:
        """One synchronous copy of the samples with ``ts >= ts_from_ns`` plus the outage intervals."""
        ring = self._store.get(pv)
        if ring is None:
            return [], []
        return ring.slice_from(ts_from_ns), ring.outages

    def open_outage(self, pv: str, now_ns: int) -> None:
        self._store.open_outage(pv, now_ns)

    def close_outage(self, pv: str, now_ns: int) -> None:
        self._store.close_outage(pv, now_ns)

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
            pre = ring.sample_before(t0_ns) if align == "last" else None

            result: list[Sample] = []
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

        sliced: dict[str, list[StoredSample]] = {}
        pre_window_samples: dict[str, StoredSample | None] = {}

        # Sorted for deterministic acquisition order (not held simultaneously)
        for pv in sorted(pvs):
            async with self._store.lock_for(pv):
                ring = self._store.get(pv)
                assert ring is not None
                ring.touch(now)
                sliced[pv] = ring.slice(t0_ns, t1_ns)
                pre_window_samples[pv] = ring.sample_before(t0_ns)

        # Re-key in caller's original order so first_input index picks the
        # caller's first PV, not the alphabetically-first.
        raw_slices = {pv: sliced[pv] for pv in pvs}

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

    def _sample_to_stored(
        self,
        sample: Sample,
        *,
        source_override: str | None = None,
        recv_ns: int = 0,
        after_connect: bool = False,
    ) -> StoredSample:
        source: Literal["live", "archive"] = (
            source_override if source_override in ("live", "archive") else sample.source
        )  # type: ignore[assignment]
        return StoredSample(
            ts_ns=sample.ts_ns,
            value=sample.value,
            severity=sample.severity,
            source=source,
            recv_ns=recv_ns,
            after_connect=after_connect,
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
