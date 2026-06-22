"""DataSource adapter: bridges Vole's BufferService to Fox's DataSource protocol.

Fox imports this class; it is constructed in fox_lifespan() and injected
into FormulaService. Fox never imports canopy.vole.gateway directly.

When a PV is not yet buffered, the adapter auto-provisions it: fetches
historical data from Mole and subscribes to live updates via Hare.
"""

from __future__ import annotations

import time
import asyncio
import logging
from collections.abc import Callable, Awaitable

import numpy as np

from canopy.fox.ports import AlignedWindow
from canopy.core.clock import Clock
from canopy.vole.errors import PVNotBuffered
from canopy.contracts.sample import Sample, BinnedPoint, AlarmSeverity
from canopy.contracts.formula import IndexMode, AlignmentMode
from canopy.epics.pv_protocol import strip_protocol_prefix
from canopy.vole.services.buffer_service import BufferService

logger = logging.getLogger(__name__)

_ALIGN_MAP: dict[AlignmentMode, str] = {
    "last": "last",
    "interpolate": "linear",
}

_INDEX_MAP: dict[IndexMode, str] = {
    "union": "union",
    "intersection": "intersection",
    "grid": "grid",
    "first_input": "first_input",
}

FetchWindowFn = Callable[..., Awaitable[list[Sample]]]
FetchBinnedFn = Callable[..., Awaitable[list[BinnedPoint]]]
SubscribeLiveFn = Callable[[list[str]], Awaitable[None]]


class VoleDataSource:
    """Implements Fox's DataSource protocol by delegating to BufferService.

    On first access to an unknown PV, provisions it by seeding from Mole
    and subscribing to Hare for live updates.
    """

    def __init__(
        self,
        buffer_service: BufferService,
        fetch_window: FetchWindowFn | None = None,
        fetch_binned: FetchBinnedFn | None = None,
        subscribe_live: SubscribeLiveFn | None = None,
        clock: Clock | None = None,
    ) -> None:
        self._buffer = buffer_service
        self._fetch_window = fetch_window
        self._fetch_binned = fetch_binned
        self._subscribe_live = subscribe_live
        self._clock = clock
        self._provisioned: set[str] = set()
        self._provisioning_locks: dict[str, asyncio.Lock] = {}

    async def read_aligned(
        self,
        pvs: list[str],
        t0_ns: int,
        t1_ns: int,
        *,
        align: AlignmentMode,
        index: IndexMode,
        grid_dt_ns: int | None = None,
    ) -> AlignedWindow:
        vole_align = _ALIGN_MAP[align]
        vole_index = _INDEX_MAP[index]
        canonical = [strip_protocol_prefix(pv) for pv in pvs]

        canon_to_orig = dict(zip(canonical, pvs))

        try:
            result = await self._buffer.read_aligned(
                canonical, t0_ns, t1_ns, align=vole_align, index=vole_index, grid_dt_ns=grid_dt_ns
            )
        except PVNotBuffered as exc:
            if not self._can_provision():
                raise
            missing_orig = [canon_to_orig.get(p, p) for p in exc.pvs]
            await self._provision(missing_orig, t0_ns, t1_ns)
            result = await self._buffer.read_aligned(
                canonical, t0_ns, t1_ns, align=vole_align, index=vole_index, grid_dt_ns=grid_dt_ns
            )

        # Re-key arrays from canonical names back to the original PV names Fox expects
        arrays = {canon_to_orig.get(k, k): v for k, v in result.arrays.items()}
        severities = {canon_to_orig.get(k, k): v for k, v in result.severities.items()}

        return AlignedWindow(
            ts_ns=result.ts_ns,
            arrays=arrays,
            severities=severities,
        )

    async def read_binned(
        self,
        pvs: list[str],
        t0_ns: int,
        t1_ns: int,
        *,
        target_buckets: int | None = None,
    ) -> AlignedWindow:
        if self._fetch_binned is None:
            from canopy.fox.errors import FormulaInputMissing

            raise FormulaInputMissing("Binned data source not available")

        per_pv: dict[str, list[BinnedPoint]] = {}
        for pv in pvs:
            points = await self._fetch_binned(pv, t0_ns, t1_ns, binning="auto", target_buckets=target_buckets)
            per_pv[pv] = points

        all_ts: set[int] = set()
        for points in per_pv.values():
            all_ts.update(p.ts_ns for p in points)
        ts_sorted = sorted(all_ts)

        if not ts_sorted:
            ts_ns = np.array([], dtype=np.int64)
            arrays = {pv: np.array([], dtype=np.float64) for pv in pvs}
            severities = {pv: np.array([], dtype=np.int64) for pv in pvs}
            return AlignedWindow(ts_ns=ts_ns, arrays=arrays, severities=severities)

        ts_ns = np.array(ts_sorted, dtype=np.int64)
        arrays: dict[str, np.ndarray] = {}
        severities: dict[str, np.ndarray] = {}

        for pv in pvs:
            points = per_pv[pv]
            pv_ts = np.array([p.ts_ns for p in points], dtype=np.int64)
            pv_vals = np.array([p.mean for p in points], dtype=np.float64)
            sev_order = list(AlarmSeverity)
            pv_sevs = np.array([sev_order.index(p.severity) for p in points], dtype=np.int64)

            if np.array_equal(pv_ts, ts_ns):
                arrays[pv] = pv_vals
                severities[pv] = pv_sevs
            else:
                aligned_vals = np.full(len(ts_ns), np.nan, dtype=np.float64)
                aligned_sevs = np.zeros(len(ts_ns), dtype=np.int64)
                indices = np.searchsorted(ts_ns, pv_ts)
                for i, idx in enumerate(indices):
                    if idx < len(ts_ns) and ts_ns[idx] == pv_ts[i]:
                        aligned_vals[idx] = pv_vals[i]
                        aligned_sevs[idx] = pv_sevs[i]
                # Forward-fill NaNs
                for i in range(1, len(aligned_vals)):
                    if np.isnan(aligned_vals[i]):
                        aligned_vals[i] = aligned_vals[i - 1]
                        aligned_sevs[i] = aligned_sevs[i - 1]
                arrays[pv] = aligned_vals
                severities[pv] = aligned_sevs

        return AlignedWindow(ts_ns=ts_ns, arrays=arrays, severities=severities)

    async def read_latest(self, pv: str) -> Sample | None:
        canonical = strip_protocol_prefix(pv)
        sample = await self._buffer.read_latest(canonical)
        if sample is None and pv not in self._provisioned and self._can_provision():
            now = self._now_ns()
            await self._provision([pv], 0, now)
            sample = await self._buffer.read_latest(canonical)
        return sample

    def _now_ns(self) -> int:
        if self._clock is not None:
            return self._clock.now_ns()
        return time.time_ns()

    def _can_provision(self) -> bool:
        return self._fetch_window is not None and self._subscribe_live is not None

    def _lock_for(self, pv: str) -> asyncio.Lock:
        if pv not in self._provisioning_locks:
            self._provisioning_locks[pv] = asyncio.Lock()
        return self._provisioning_locks[pv]

    async def _provision(self, pvs: list[str], t0_ns: int, t1_ns: int) -> None:
        """Seed missing PVs from Mole and subscribe to Hare live updates."""
        assert self._fetch_window is not None
        assert self._subscribe_live is not None

        to_provision = [pv for pv in pvs if pv not in self._provisioned]
        if not to_provision:
            return

        now = self._now_ns()
        if t1_ns == 0:
            t1_ns = now

        seeded: set[str] = set()
        for pv in to_provision:
            async with self._lock_for(pv):
                if pv in self._provisioned:
                    continue
                try:
                    samples = await self._fetch_window(pv, t0_ns, t1_ns, max_points=self._buffer.max_points)
                    if samples:
                        stored = await self._buffer.seed(pv, samples)
                        logger.info("Auto-provisioned PV %s: seeded %d/%d samples from Mole", pv, stored, len(samples))
                    else:
                        await self._buffer.seed(pv, [])
                        logger.info("Auto-provisioned PV %s: no historical data in Mole", pv)
                    seeded.add(pv)
                except Exception:
                    logger.info("PV %s not in archiver, subscribing to live only", pv)

        await self._subscribe_live(to_provision)
        self._provisioned.update(seeded)
