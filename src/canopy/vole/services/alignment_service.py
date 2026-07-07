"""Cross-PV time alignment.

Given raw sliced samples per PV and alignment parameters, produces a common
time index with aligned value/severity arrays suitable for Fox's formula eval.
"""

from __future__ import annotations

import bisect
from typing import Literal
from dataclasses import dataclass

import numpy as np

from canopy.vole.errors import WindowTooLarge
from canopy.contracts.sample import AlarmSeverity
from canopy.vole.buffer.ring import StoredSample

MAX_GRID_POINTS = 10_000_000

AlignMode = Literal["nearest", "last", "linear", "none"]
IndexMode = Literal["union", "intersection", "grid", "first_input"]

_SEVERITY_ORDINAL: dict[AlarmSeverity, int] = {
    AlarmSeverity.NO_ALARM: 0,
    AlarmSeverity.MINOR: 1,
    AlarmSeverity.MAJOR: 2,
    AlarmSeverity.INVALID: 3,
}


@dataclass(frozen=True)
class AlignedResult:
    """Result of alignment: common time index + per-PV arrays."""

    ts_ns: np.ndarray
    arrays: dict[str, np.ndarray]
    severities: dict[str, np.ndarray]


class AlignmentService:
    """Stateless service: computes aligned arrays from raw per-PV sample slices."""

    def align(
        self,
        raw_slices: dict[str, list[StoredSample]],
        t0_ns: int,
        t1_ns: int,
        *,
        align: AlignMode = "last",
        index: IndexMode | str = "union",
        grid_dt_ns: int | None = None,
        pre_window_samples: dict[str, StoredSample | None] | None = None,
    ) -> AlignedResult:
        """Produce aligned arrays from raw sliced samples."""
        index_ts = self._compute_index(raw_slices, t0_ns, t1_ns, index=index, grid_dt_ns=grid_dt_ns)

        if pre_window_samples is None:
            pre_window_samples = {}

        arrays: dict[str, np.ndarray] = {}
        severities: dict[str, np.ndarray] = {}

        for pv, samples in raw_slices.items():
            pre = pre_window_samples.get(pv)
            values, sevs = self._align_pv(samples, index_ts, align=align, pre_window_sample=pre)
            arrays[pv] = values
            severities[pv] = sevs

        return AlignedResult(ts_ns=index_ts, arrays=arrays, severities=severities)

    def _compute_index(
        self,
        raw_slices: dict[str, list[StoredSample]],
        t0_ns: int,
        t1_ns: int,
        *,
        index: IndexMode | str,
        grid_dt_ns: int | None,
    ) -> np.ndarray:
        if index == "grid":
            if grid_dt_ns is None or grid_dt_ns <= 0:
                raise WindowTooLarge("index='grid' requires grid_dt_ns > 0")
            n_points = (t1_ns - t0_ns) // grid_dt_ns + 1
            if n_points > MAX_GRID_POINTS:
                raise WindowTooLarge(
                    f"Grid would produce {n_points} points (max {MAX_GRID_POINTS}); "
                    f"increase grid_dt_ns or narrow the time window"
                )
            return np.arange(t0_ns, t1_ns + 1, grid_dt_ns, dtype=np.int64)

        if index == "first_input":
            first_pv = next(iter(raw_slices), None)
            if first_pv is None:
                return np.array([], dtype=np.int64)
            return np.array([s.ts_ns for s in raw_slices[first_pv]], dtype=np.int64)

        if index == "intersection":
            if not raw_slices:
                return np.array([], dtype=np.int64)
            sets = [set(s.ts_ns for s in samples) for samples in raw_slices.values()]
            common = sets[0]
            for s in sets[1:]:
                common &= s
            return np.array(sorted(common), dtype=np.int64)

        # Default: "union"
        all_ts: set[int] = set()
        for samples in raw_slices.values():
            for s in samples:
                all_ts.add(s.ts_ns)
        return np.array(sorted(all_ts), dtype=np.int64)

    def _align_pv(
        self,
        samples: list[StoredSample],
        index_ts: np.ndarray,
        *,
        align: AlignMode,
        pre_window_sample: StoredSample | None = None,
    ) -> tuple[np.ndarray, np.ndarray]:
        """Align one PV's samples to the common index."""
        n = len(index_ts)
        values = np.full(n, np.nan, dtype=np.float64)
        sevs = np.full(n, _SEVERITY_ORDINAL[AlarmSeverity.INVALID], dtype=np.int64)

        if n == 0:
            return values, sevs

        ts_list = [s.ts_ns for s in samples]

        if align == "last":
            self._align_last(samples, ts_list, index_ts, values, sevs, pre_window_sample)
        elif align == "linear":
            self._align_linear(samples, ts_list, index_ts, values, sevs, pre_window_sample)
        elif align == "nearest":
            self._align_nearest(samples, ts_list, index_ts, values, sevs)
        elif align == "none":
            self._align_none(samples, ts_list, index_ts, values, sevs)

        return values, sevs

    def _align_last(
        self,
        samples: list[StoredSample],
        ts_list: list[int],
        index_ts: np.ndarray,
        values: np.ndarray,
        sevs: np.ndarray,
        pre_window_sample: StoredSample | None,
    ) -> None:
        """Forward-fill: use most recent sample at or before each index point."""
        for i, t in enumerate(index_ts):
            idx = bisect.bisect_right(ts_list, t) - 1
            if idx >= 0:
                s = samples[idx]
                values[i] = self._to_float(s.value)
                sevs[i] = _SEVERITY_ORDINAL[s.severity]
            elif pre_window_sample is not None:
                values[i] = self._to_float(pre_window_sample.value)
                sevs[i] = _SEVERITY_ORDINAL[pre_window_sample.severity]

    def _align_linear(
        self,
        samples: list[StoredSample],
        ts_list: list[int],
        index_ts: np.ndarray,
        values: np.ndarray,
        sevs: np.ndarray,
        pre_window_sample: StoredSample | None,
    ) -> None:
        """Linear interpolation between bracketing samples."""
        for i, t in enumerate(index_ts):
            idx = bisect.bisect_right(ts_list, t)

            if idx > 0 and idx < len(samples):
                # Between two samples — interpolate
                s_lo = samples[idx - 1]
                s_hi = samples[idx]
                if s_lo.ts_ns == t:
                    values[i] = self._to_float(s_lo.value)
                    sevs[i] = _SEVERITY_ORDINAL[s_lo.severity]
                else:
                    v_lo = self._to_float(s_lo.value)
                    v_hi = self._to_float(s_hi.value)
                    if np.isnan(v_lo) or np.isnan(v_hi):
                        values[i] = np.nan
                    else:
                        frac = (t - s_lo.ts_ns) / (s_hi.ts_ns - s_lo.ts_ns)
                        values[i] = v_lo + frac * (v_hi - v_lo)
                    sevs[i] = max(
                        _SEVERITY_ORDINAL[s_lo.severity],
                        _SEVERITY_ORDINAL[s_hi.severity],
                    )
            elif idx > 0:
                # Past last sample — use last (no extrapolation)
                s = samples[idx - 1]
                if s.ts_ns == t:
                    values[i] = self._to_float(s.value)
                    sevs[i] = _SEVERITY_ORDINAL[s.severity]
                # else: NaN (no extrapolation beyond last)
            elif idx == 0 and pre_window_sample is not None and samples:
                # Before first sample but have pre-window — interpolate from pre
                s_lo = pre_window_sample
                s_hi = samples[0]
                v_lo = self._to_float(s_lo.value)
                v_hi = self._to_float(s_hi.value)
                if np.isnan(v_lo) or np.isnan(v_hi):
                    values[i] = np.nan
                else:
                    frac = (t - s_lo.ts_ns) / (s_hi.ts_ns - s_lo.ts_ns)
                    values[i] = v_lo + frac * (v_hi - v_lo)
                sevs[i] = max(
                    _SEVERITY_ORDINAL[s_lo.severity],
                    _SEVERITY_ORDINAL[s_hi.severity],
                )
            elif idx == 0 and pre_window_sample is not None:
                # No samples at all, only pre-window — hold value
                values[i] = self._to_float(pre_window_sample.value)
                sevs[i] = _SEVERITY_ORDINAL[pre_window_sample.severity]

    def _align_nearest(
        self,
        samples: list[StoredSample],
        ts_list: list[int],
        index_ts: np.ndarray,
        values: np.ndarray,
        sevs: np.ndarray,
    ) -> None:
        """Use closest sample by absolute time difference."""
        if not samples:
            return
        for i, t in enumerate(index_ts):
            idx = bisect.bisect_left(ts_list, t)
            candidates: list[tuple[int, StoredSample]] = []
            if idx < len(samples):
                candidates.append((abs(samples[idx].ts_ns - t), samples[idx]))
            if idx > 0:
                candidates.append((abs(samples[idx - 1].ts_ns - t), samples[idx - 1]))
            if candidates:
                _, best = min(candidates, key=lambda c: c[0])
                values[i] = self._to_float(best.value)
                sevs[i] = _SEVERITY_ORDINAL[best.severity]

    def _align_none(
        self,
        samples: list[StoredSample],
        ts_list: list[int],
        index_ts: np.ndarray,
        values: np.ndarray,
        sevs: np.ndarray,
    ) -> None:
        """Only exact timestamp matches produce values."""
        ts_set = {s.ts_ns: s for s in samples}
        for i, t in enumerate(index_ts):
            if t in ts_set:
                s = ts_set[t]
                values[i] = self._to_float(s.value)
                sevs[i] = _SEVERITY_ORDINAL[s.severity]

    @staticmethod
    def _to_float(value: float | int | bool | str | bytes | None) -> float:
        if value is None:
            return np.nan
        if isinstance(value, bool):
            return float(value)
        if isinstance(value, int | float):
            return float(value)
        try:
            return float(value)
        except (ValueError, TypeError):
            return np.nan
