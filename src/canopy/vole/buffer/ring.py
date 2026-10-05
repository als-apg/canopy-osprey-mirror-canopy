from __future__ import annotations

import sys
import bisect
from typing import Literal, NamedTuple

from canopy.contracts.sample import AlarmSeverity


class StoredSample(NamedTuple):
    ts_ns: int
    value: float | int | bool | str | bytes | None
    severity: AlarmSeverity
    source: Literal["live", "archive"]
    # Server receive time of the Hare flush that carried the sample; 0 for archive-seeded samples.
    recv_ns: int = 0
    # True for the first sample after a (re)connect: an ``initial`` frame, the first
    # sample after a disconnected entry, or a PV's first sample at all.
    after_connect: bool = False


def measure_sample_bytes() -> int:
    """The footprint of one stored sample: the six-field tuple plus its per-sample objects.

    The severity enum member, the source literal and the bool are shared singletons;
    the timestamp, a float value and the receive time are allocated per sample.
    """
    ts = 1_700_000_000_000_000_000
    sample = StoredSample(ts, 1.5, AlarmSeverity.NO_ALARM, "live", ts, False)
    return (
        sys.getsizeof(sample)
        + sys.getsizeof(sample.ts_ns)
        + sys.getsizeof(sample.value)
        + sys.getsizeof(sample.recv_ns)
    )


# Measured, not assumed: str/bytes payloads are still under-counted.
BYTES_PER_SAMPLE = measure_sample_bytes()

Outage = tuple[int, int | None]


class PerPVRing:
    """Bounded, time-ordered, append-only ring for one PV.

    Backed by a sorted list with bisect for O(log N) lookups.
    FIFO eviction enforced on every mutation when ceilings are exceeded.

    Also carries the per-PV state that lives and dies with the ring: whether its
    archive history has been provisioned (and when a failed attempt may be
    retried), and the outage intervals ingest recorded on the receive clock.
    """

    def __init__(self, *, max_points: int) -> None:
        self._samples: list[StoredSample] = []
        self._max_points = max_points
        self._last_access_ns: int = 0
        self._dropped_out_of_order: int = 0
        self.provisioned: bool = False
        self.provision_retry_after_ns: int = 0
        self._outages: list[tuple[int, int]] = []
        self._open_outage_since_ns: int | None = None

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
        self._enforce_retention()
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
        self._enforce_retention()
        return len(to_insert)

    def slice(self, t0_ns: int, t1_ns: int) -> list[StoredSample]:
        """Return samples in [t0_ns, t1_ns] inclusive."""
        if not self._samples:
            return []
        lo = bisect.bisect_left(self._samples, t0_ns, key=lambda s: s.ts_ns)
        hi = bisect.bisect_right(self._samples, t1_ns, key=lambda s: s.ts_ns)
        return self._samples[lo:hi]

    def slice_from(self, ts_from_ns: int) -> list[StoredSample]:
        """One O(k) copy of the samples with ``ts_ns >= ts_from_ns``."""
        if not self._samples:
            return []
        lo = bisect.bisect_left(self._samples, ts_from_ns, key=lambda s: s.ts_ns)
        return self._samples[lo:]

    def sample_before(self, ts_ns: int) -> StoredSample | None:
        """Return the last sample with ts_ns < given ts."""
        if not self._samples:
            return None
        idx = bisect.bisect_left(self._samples, ts_ns, key=lambda s: s.ts_ns)
        if idx == 0:
            return None
        return self._samples[idx - 1]

    def last_at_or_before(self, ts_ns: int) -> StoredSample | None:
        """Return the last sample with ts_ns <= given ts."""
        if not self._samples:
            return None
        idx = bisect.bisect_right(self._samples, ts_ns, key=lambda s: s.ts_ns)
        if idx == 0:
            return None
        return self._samples[idx - 1]

    def samples_since(self, recv_after_ns: int) -> list[StoredSample]:
        """Every live sample received after ``recv_after_ns``, oldest first.

        Scans back from the newest sample and stops at the first one received at
        or before the argument -- archive-seeded samples carry 0, so the scan is
        O(new samples) and never walks the seeded history.
        """
        out: list[StoredSample] = []
        for s in reversed(self._samples):
            if s.recv_ns <= recv_after_ns:
                break
            out.append(s)
        out.reverse()
        return out

    # --- outages (receive clock) ------------------------------------------ #

    @property
    def outages(self) -> list[Outage]:
        """Closed intervals ``(start_ns, end_ns)`` plus the open one as ``(start_ns, None)``."""
        intervals: list[Outage] = list(self._outages)
        if self._open_outage_since_ns is not None:
            intervals.append((self._open_outage_since_ns, None))
        return intervals

    @property
    def outage_open(self) -> bool:
        """True while an outage is open (the PV's connection is down now)."""
        return self._open_outage_since_ns is not None

    def open_outage(self, now_ns: int) -> None:
        if self._open_outage_since_ns is None:
            self._open_outage_since_ns = now_ns

    def close_outage(self, now_ns: int) -> None:
        if self._open_outage_since_ns is not None:
            self._outages.append((self._open_outage_since_ns, max(now_ns, self._open_outage_since_ns)))
            self._open_outage_since_ns = None

    def in_outage(self, ts_ns: int) -> bool:
        if self._open_outage_since_ns is not None and ts_ns >= self._open_outage_since_ns:
            return True
        return any(start <= ts_ns <= end for start, end in self._outages)

    # --- capacity ----------------------------------------------------------- #

    def clear(self) -> None:
        self._samples.clear()

    def update_ceilings(self, *, max_points: int | None = None) -> None:
        if max_points is not None:
            self._max_points = max_points

    def ensure_capacity(self, max_points: int) -> None:
        """Grow-only: never lowers an existing ceiling."""
        if max_points > self._max_points:
            self._max_points = max_points

    def shrink_to(self, max_points: int) -> int:
        """Lower the ceiling, trim to the newest ``max_points`` and forget the provisioned history.

        Returns the number of samples trimmed. Used only under memory pressure.
        """
        before = len(self._samples)
        self._max_points = max_points
        self._enforce_retention()
        self.provisioned = False
        return before - len(self._samples)

    def _enforce_retention(self) -> None:
        if len(self._samples) > self._max_points:
            excess = len(self._samples) - self._max_points
            del self._samples[:excess]
