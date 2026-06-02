"""Tests for VoleDataSource adapter."""

import numpy as np
import pytest

from canopy.vole.adapter import VoleDataSource
from canopy.contracts.sample import Sample, AlarmSeverity

# FakeClock starts at 1_000_000_000_000, max_age_ns=60_000_000_000
_BASE = 1_000_000_000_000 - 50_000_000_000


class TestVoleDataSource:
    async def test_read_aligned_returns_aligned_window(self, test_service):
        samples = [
            Sample(ts_ns=_BASE + 100, value=10.0, severity=AlarmSeverity.NO_ALARM, source="archive"),
            Sample(ts_ns=_BASE + 200, value=20.0, severity=AlarmSeverity.NO_ALARM, source="archive"),
            Sample(ts_ns=_BASE + 300, value=30.0, severity=AlarmSeverity.MINOR, source="archive"),
        ]
        await test_service.seed("PV:A", samples)

        adapter = VoleDataSource(test_service)
        window = await adapter.read_aligned(["PV:A"], _BASE + 100, _BASE + 300, align="last", index="union")

        assert isinstance(window.ts_ns, np.ndarray)
        assert "PV:A" in window.arrays
        assert "PV:A" in window.severities
        assert len(window.arrays["PV:A"]) == len(window.ts_ns)
        assert len(window.severities["PV:A"]) == len(window.ts_ns)

    async def test_read_aligned_maps_interpolate_to_linear(self, test_service):
        samples = [
            Sample(ts_ns=_BASE + 100, value=10.0, severity=AlarmSeverity.NO_ALARM, source="archive"),
            Sample(ts_ns=_BASE + 300, value=30.0, severity=AlarmSeverity.NO_ALARM, source="archive"),
        ]
        await test_service.seed("PV:A", samples)

        adapter = VoleDataSource(test_service)
        window = await adapter.read_aligned(
            ["PV:A"], _BASE + 100, _BASE + 300, align="interpolate", index="grid", grid_dt_ns=100
        )

        # Grid: [_BASE+100, _BASE+200, _BASE+300]
        assert len(window.ts_ns) == 3
        vals = window.arrays["PV:A"]
        assert vals[0] == pytest.approx(10.0)
        assert vals[1] == pytest.approx(20.0)
        assert vals[2] == pytest.approx(30.0)

    async def test_read_latest(self, test_service):
        sample = Sample(ts_ns=_BASE + 500, value=42.0, severity=AlarmSeverity.NO_ALARM, source="archive")
        await test_service.seed("PV:A", [sample])

        adapter = VoleDataSource(test_service)
        result = await adapter.read_latest("PV:A")
        assert result is not None
        assert result.value == 42.0

    async def test_read_latest_unknown_pv(self, test_service):
        adapter = VoleDataSource(test_service)
        result = await adapter.read_latest("PV:UNKNOWN")
        assert result is None
