"""Integration tests for BufferService."""

import numpy as np
import pytest

from canopy.vole.errors import PVNotBuffered
from canopy.contracts.sample import Sample, AlarmSeverity

# FakeClock starts at 1_000_000_000_000 (conftest), max_age_ns=60_000_000_000
# Samples must be within 60s of clock start to survive retention.
_BASE = 1_000_000_000_000 - 50_000_000_000  # 50s before clock start


def _sample(ts_ns: int, value: float = 1.0, source: str = "archive") -> Sample:
    return Sample(ts_ns=_BASE + ts_ns, value=value, severity=AlarmSeverity.NO_ALARM, source=source)


class TestSeedAndRead:
    async def test_seed_then_read_latest(self, test_service):
        samples = [_sample(100, 10.0), _sample(200, 20.0), _sample(300, 30.0)]
        count = await test_service.seed("PV:A", samples)
        assert count == 3

        latest = await test_service.read_latest("PV:A")
        assert latest is not None
        assert latest.ts_ns == _BASE + 300
        assert latest.value == 30.0

    async def test_seed_then_read_window(self, test_service):
        samples = [_sample(i * 100, float(i)) for i in range(10)]
        await test_service.seed("PV:A", samples)

        window = await test_service.read_window("PV:A", _BASE + 200, _BASE + 700)
        assert len(window) >= 5
        assert window[0].ts_ns >= _BASE + 200
        assert window[-1].ts_ns <= _BASE + 700

    async def test_seed_then_read_aligned(self, test_service):
        samples_a = [_sample(i * 100, float(i)) for i in range(5)]
        samples_b = [_sample(i * 100, float(i * 2)) for i in range(5)]
        await test_service.seed("PV:A", samples_a)
        await test_service.seed("PV:B", samples_b)

        result = await test_service.read_aligned(["PV:A", "PV:B"], _BASE, _BASE + 400, align="last", index="union")
        assert len(result.ts_ns) > 0
        assert "PV:A" in result.arrays
        assert "PV:B" in result.arrays
        assert len(result.arrays["PV:A"]) == len(result.ts_ns)
        assert len(result.arrays["PV:B"]) == len(result.ts_ns)


class TestPVNotBuffered:
    async def test_read_window_raises(self, test_service):
        with pytest.raises(PVNotBuffered):
            await test_service.read_window("PV:NONEXISTENT", _BASE, _BASE + 1000)

    async def test_read_aligned_raises_with_missing_pvs(self, test_service):
        await test_service.seed("PV:A", [_sample(100)])
        with pytest.raises(PVNotBuffered, match="PV:B"):
            await test_service.read_aligned(["PV:A", "PV:B"], _BASE, _BASE + 1000)

    async def test_read_latest_returns_none_for_unknown(self, test_service):
        result = await test_service.read_latest("PV:UNKNOWN")
        assert result is None


class TestAppend:
    async def test_append_and_read_latest(self, test_service):
        sample = Sample(ts_ns=_BASE + 500, value=42.0, severity=AlarmSeverity.MINOR, source="live")
        await test_service.append("PV:LIVE", sample)

        latest = await test_service.read_latest("PV:LIVE")
        assert latest is not None
        assert latest.ts_ns == _BASE + 500
        assert latest.value == 42.0
        assert latest.source == "live"


class TestDrop:
    async def test_drop_removes_ring(self, test_service):
        await test_service.seed("PV:A", [_sample(100)])
        await test_service.drop("PV:A")

        with pytest.raises(PVNotBuffered):
            await test_service.read_window("PV:A", 0, 1000)

    async def test_drop_idempotent(self, test_service):
        await test_service.drop("PV:NONEXISTENT")


class TestStats:
    async def test_stats_empty(self, test_service):
        stats = await test_service.stats()
        assert stats.ring_count == 0
        assert stats.total_points == 0
        assert stats.total_bytes == 0

    async def test_stats_after_seed(self, test_service):
        await test_service.seed("PV:A", [_sample(100), _sample(200)])
        stats = await test_service.stats()
        assert stats.ring_count == 1
        assert stats.total_points == 2
        assert stats.total_bytes > 0


class TestReadAlignedGrid:
    async def test_grid_alignment(self, test_service):
        samples = [_sample(100, 10.0), _sample(300, 30.0)]
        await test_service.seed("PV:A", samples)

        result = await test_service.read_aligned(
            ["PV:A"], _BASE, _BASE + 400, align="last", index="grid", grid_dt_ns=100
        )
        # Grid: [_BASE, _BASE+100, _BASE+200, _BASE+300, _BASE+400]
        assert len(result.ts_ns) == 5
        vals = result.arrays["PV:A"]
        assert np.isnan(vals[0])  # no sample before _BASE
        assert vals[1] == 10.0  # sample at _BASE+100
        assert vals[2] == 10.0  # forward-fill
        assert vals[3] == 30.0  # sample at _BASE+300
        assert vals[4] == 30.0  # forward-fill
