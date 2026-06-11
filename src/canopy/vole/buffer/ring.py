from __future__ import annotations

import bisect
from typing import Literal, NamedTuple

from canopy.contracts.sample import AlarmSeverity


class StoredSample(NamedTuple):
    ts_ns: int
    value: float | int | bool | str | bytes | None
    severity: AlarmSeverity
    source: Literal["live", "archive"]


BYTES_PER_SAMPLE = 80


class PerPVRing:
    """Bounded, time-ordered, append-only ring for one PV.

    Backed by a sorted list with bisect for O(log N) lookups.
    FIFO eviction enforced on every mutation when ceilings are exceeded.
    """

    def __init__(self, *, max_age_ns: int, max_points: int) -> None:
        self._samples: list[StoredSample] = []
        self._max_age_ns = max_age_ns
        self._max_points = max_points
        self._last_access_ns: int = 0
        self._dropped_out_of_order: int = 0

    @property
    def max_age_ns(self) -> int:
        return self._max_age_ns

    @property
    def max_points(self) -> int:
        return self._max_points

    @property
    def count(self) -> int:
        return len(self._samples)

    @property
    def byte_estimate(self) -> int:
        return len(self._samples) * BYTES_PER_SAMPLE

    @property
    def latest(self) -> StoredSample | None:
        return self._samples[-1] if self._samples else None

    @property
    def earliest(self) -> StoredSample | None:
        return self._samples[0] if self._samples else None

    @property
    def last_access_ns(self) -> int:
        return self._last_access_ns

    @property
    def dropped_out_of_order(self) -> int:
        return self._dropped_out_of_order

    def touch(self, now_ns: int) -> None:
        self._last_access_ns = now_ns

    def append(self, sample: StoredSample, now_ns: int) -> bool:
        """Append one sample. Returns False if out-of-order (dropped)."""
        if self._samples and sample.ts_ns <= self._samples[-1].ts_ns:
            self._dropped_out_of_order += 1
            return False
        self._samples.append(sample)
        self._last_access_ns = now_ns
        self._enforce_retention(now_ns)
        return True

    def seed(self, samples: list[StoredSample], now_ns: int) -> int:
        """Bulk insert sorted archive samples. Returns count actually stored.

        Samples whose ts_ns overlaps existing live data are discarded (live wins).
        """
        if not samples:
            return 0

        earliest_live_ts = None
        for s in self._samples:
            if s.source == "live":
                earliest_live_ts = s.ts_ns
                break

        to_insert: list[StoredSample] = []
        for s in samples:
            if earliest_live_ts is not None and s.ts_ns >= earliest_live_ts:
                continue
            to_insert.append(s)

        if not to_insert:
            return 0

        merged = sorted(self._samples + to_insert, key=lambda s: s.ts_ns)
        # Deduplicate: keep last occurrence at each timestamp (live wins over archive)
        deduped: list[StoredSample] = []
        seen_ts: set[int] = set()
        for s in reversed(merged):
            if s.ts_ns not in seen_ts:
                seen_ts.add(s.ts_ns)
                deduped.append(s)
        deduped.reverse()
        self._samples = deduped

        self._last_access_ns = now_ns
        self._enforce_retention(now_ns)
        return len(to_insert)

    def slice(self, t0_ns: int, t1_ns: int) -> list[StoredSample]:
        """Return samples in [t0_ns, t1_ns] inclusive."""
        if not self._samples:
            return []
        lo = bisect.bisect_left(self._samples, t0_ns, key=lambda s: s.ts_ns)
        hi = bisect.bisect_right(self._samples, t1_ns, key=lambda s: s.ts_ns)
        return self._samples[lo:hi]

    def sample_before(self, ts_ns: int) -> StoredSample | None:
        """Return the last sample with ts_ns < given ts."""
        if not self._samples:
            return None
        idx = bisect.bisect_left(self._samples, ts_ns, key=lambda s: s.ts_ns)
        if idx == 0:
            return None
        return self._samples[idx - 1]

    def clear(self) -> None:
        self._samples.clear()

    def update_ceilings(self, *, max_age_ns: int | None = None, max_points: int | None = None) -> None:
        if max_age_ns is not None:
            self._max_age_ns = max_age_ns
        if max_points is not None:
            self._max_points = max_points

    def _enforce_retention(self, now_ns: int) -> None:
        if len(self._samples) > self._max_points:
            excess = len(self._samples) - self._max_points
            del self._samples[:excess]

        if self._max_age_ns > 0 and self._samples:
            cutoff = now_ns - self._max_age_ns
            idx = bisect.bisect_left(self._samples, cutoff, key=lambda s: s.ts_ns)
            if idx > 0:
                del self._samples[:idx]
