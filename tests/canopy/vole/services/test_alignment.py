"""Unit tests for AlignmentService."""

import numpy as np
import pytest

from canopy.contracts.sample import AlarmSeverity
from canopy.vole.buffer.ring import StoredSample
from canopy.vole.services.alignment_service import AlignmentService


@pytest.fixture
def svc():
    return AlignmentService()


def _s(ts_ns: int, value: float = 1.0, sev: AlarmSeverity = AlarmSeverity.NO_ALARM) -> StoredSample:
    return StoredSample(ts_ns=ts_ns, value=value, severity=sev, source="archive")


class TestIndexModes:
    def test_union_index(self, svc):
        raw = {
            "A": [_s(100), _s(300)],
            "B": [_s(200), _s(300)],
        }
        result = svc.align(raw, 0, 1000, align="last", index="union")
        assert list(result.ts_ns) == [100, 200, 300]

    def test_intersection_index(self, svc):
        raw = {
            "A": [_s(100), _s(200), _s(300)],
            "B": [_s(200), _s(300), _s(400)],
        }
        result = svc.align(raw, 0, 1000, align="last", index="intersection")
        assert list(result.ts_ns) == [200, 300]

    def test_grid_index(self, svc):
        raw = {"A": [_s(100), _s(250)]}
        result = svc.align(raw, 0, 500, align="last", index="grid", grid_dt_ns=100)
        expected = [0, 100, 200, 300, 400, 500]
        assert list(result.ts_ns) == expected

    def test_grid_without_dt_raises(self, svc):
        with pytest.raises(ValueError, match="grid_dt_ns"):
            svc.align({"A": []}, 0, 1000, align="last", index="grid")

    def test_first_input_index(self, svc):
        raw = {
            "A": [_s(100), _s(300), _s(500)],
            "B": [_s(200), _s(400)],
        }
        result = svc.align(raw, 0, 1000, align="last", index="first_input")
        assert list(result.ts_ns) == [100, 300, 500]


class TestAlignLast:
    def test_forward_fill(self, svc):
        raw = {"A": [_s(100, 10.0), _s(300, 30.0)]}
        result = svc.align(raw, 0, 500, align="last", index="grid", grid_dt_ns=100)
        # Index: [0, 100, 200, 300, 400, 500]
        # At 0: no sample before → NaN
        # At 100: use sample at 100 → 10.0
        # At 200: forward-fill from 100 → 10.0
        # At 300: use sample at 300 → 30.0
        # At 400, 500: forward-fill from 300 → 30.0
        vals = result.arrays["A"]
        assert np.isnan(vals[0])
        assert vals[1] == 10.0
        assert vals[2] == 10.0
        assert vals[3] == 30.0
        assert vals[4] == 30.0
        assert vals[5] == 30.0

    def test_forward_fill_with_pre_window(self, svc):
        raw = {"A": [_s(300, 30.0)]}
        pre = {"A": _s(50, 5.0)}
        result = svc.align(
            raw,
            100,
            400,
            align="last",
            index="grid",
            grid_dt_ns=100,
            pre_window_samples=pre,
        )
        # Index: [100, 200, 300, 400]
        # At 100: no sample in window before, but pre_window=5.0
        # At 200: forward-fill from pre_window → 5.0
        # At 300: sample → 30.0
        # At 400: forward-fill → 30.0
        vals = result.arrays["A"]
        assert vals[0] == 5.0
        assert vals[1] == 5.0
        assert vals[2] == 30.0
        assert vals[3] == 30.0


class TestAlignLinear:
    def test_interpolation(self, svc):
        raw = {"A": [_s(100, 10.0), _s(300, 30.0)]}
        result = svc.align(raw, 100, 300, align="linear", index="grid", grid_dt_ns=100)
        # Index: [100, 200, 300]
        # At 100: exact match → 10.0
        # At 200: interpolate between 10 and 30 → 20.0
        # At 300: exact match → 30.0
        vals = result.arrays["A"]
        assert vals[0] == pytest.approx(10.0)
        assert vals[1] == pytest.approx(20.0)
        assert vals[2] == pytest.approx(30.0)

    def test_no_extrapolation_beyond_last(self, svc):
        raw = {"A": [_s(100, 10.0), _s(200, 20.0)]}
        result = svc.align(raw, 100, 400, align="linear", index="grid", grid_dt_ns=100)
        # At 300, 400: beyond last sample → NaN (no extrapolation)
        vals = result.arrays["A"]
        assert vals[0] == pytest.approx(10.0)
        assert vals[1] == pytest.approx(20.0)
        assert np.isnan(vals[2])
        assert np.isnan(vals[3])


class TestAlignNearest:
    def test_nearest(self, svc):
        raw = {"A": [_s(100, 10.0), _s(300, 30.0)]}
        result = svc.align(raw, 0, 400, align="nearest", index="grid", grid_dt_ns=100)
        # Index: [0, 100, 200, 300, 400]
        # 0 → nearest is 100 (dist=100)
        # 100 → exact 100
        # 200 → equidistant from 100 and 300, pick 100 (lower dist wins via min)
        # 300 → exact 300
        # 400 → nearest is 300 (dist=100)
        vals = result.arrays["A"]
        assert vals[0] == 10.0
        assert vals[1] == 10.0
        assert vals[3] == 30.0
        assert vals[4] == 30.0


class TestAlignNone:
    def test_exact_match_only(self, svc):
        raw = {"A": [_s(100, 10.0), _s(200, 20.0), _s(300, 30.0)]}
        result = svc.align(raw, 0, 500, align="none", index="grid", grid_dt_ns=100)
        # Index: [0, 100, 200, 300, 400, 500]
        vals = result.arrays["A"]
        assert np.isnan(vals[0])
        assert vals[1] == 10.0
        assert vals[2] == 20.0
        assert vals[3] == 30.0
        assert np.isnan(vals[4])
        assert np.isnan(vals[5])


class TestSeverityEncoding:
    def test_severity_ordinals(self, svc):
        raw = {
            "A": [
                _s(100, sev=AlarmSeverity.NO_ALARM),
                _s(200, sev=AlarmSeverity.MINOR),
                _s(300, sev=AlarmSeverity.MAJOR),
            ]
        }
        result = svc.align(raw, 100, 300, align="none", index="union")
        sevs = result.severities["A"]
        assert list(sevs) == [0, 1, 2]


class TestEdgeCases:
    def test_empty_input(self, svc):
        result = svc.align({}, 0, 1000, align="last", index="union")
        assert len(result.ts_ns) == 0
        assert result.arrays == {}

    def test_pv_with_no_samples(self, svc):
        raw = {"A": [], "B": [_s(100, 10.0)]}
        result = svc.align(raw, 0, 200, align="last", index="union")
        # Index from B: [100]
        assert list(result.ts_ns) == [100]
        assert np.isnan(result.arrays["A"][0])
        assert result.arrays["B"][0] == 10.0
