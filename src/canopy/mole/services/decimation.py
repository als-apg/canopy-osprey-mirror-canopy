"""Uniform-time downsampling: for plot density control, NOT statistical aggregation.

Used as a fallback in `ArchiverService.fetch_window` when the adapter returned
more samples than the caller asked for via `maxPoints`. For statistical
aggregation, use binned mode (`fetch_binned`).
"""

import bisect

from canopy.contracts.sample import Sample


def uniform_time_decimate(samples: list[Sample], max_points: int) -> list[Sample]:
    """Pick samples nearest each of `max_points` evenly-spaced timestamps.

    Preserves time-ordering and never duplicates a sample. If the input has
    fewer points than `max_points`, returns it unchanged. Edge cases:

    - `max_points <= 0`: returns `[]`.
    - `max_points == 1` or single sample: returns `[samples[0]]`.
    - All samples share one timestamp: returns `[samples[0]]`.
    """
    if max_points <= 0:
        return []
    n = len(samples)
    if n <= max_points:
        return list(samples)
    if max_points == 1 or n == 1:
        return [samples[0]]

    t0 = samples[0].ts_ns
    t1 = samples[-1].ts_ns
    if t1 == t0:
        return [samples[0]]

    span = t1 - t0
    out: list[Sample] = []
    seen: set[int] = set()
    for i in range(max_points):
        target = t0 + (i * span) // (max_points - 1)
        idx = _nearest_index(samples, target)
        if idx not in seen:
            seen.add(idx)
            out.append(samples[idx])
    return out


def _nearest_index(samples: list[Sample], target_ns: int) -> int:
    pos = bisect.bisect_left(samples, target_ns, key=lambda s: s.ts_ns)
    if pos == 0:
        return 0
    if pos == len(samples):
        return len(samples) - 1
    before = samples[pos - 1]
    after = samples[pos]
    if abs(before.ts_ns - target_ns) <= abs(after.ts_ns - target_ns):
        return pos - 1
    return pos
