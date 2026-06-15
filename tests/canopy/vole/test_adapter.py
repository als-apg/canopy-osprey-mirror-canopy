"""Tests for VoleDataSource adapter."""

import numpy as np
import pytest

from canopy.vole.errors import PVNotBuffered
from canopy.vole.adapter import VoleDataSource
from canopy.contracts.sample import Sample, AlarmSeverity

# FakeClock starts at 1_000_000_000_000, max_points=1000
_BASE = 1_000_000_000_000 - 50_000_000_000


def _make_samples(n: int = 5) -> list[Sample]:
    return [
        Sample(ts_ns=_BASE + i * 100, value=float(i), severity=AlarmSeverity.NO_ALARM, source="archive")
        for i in range(1, n + 1)
    ]


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


class TestAutoProvision:
    """Tests for auto-provisioning missing PVs from Mole."""

    async def test_read_aligned_provisions_missing_pv(self, test_service, fake_clock):
        """A read for an unknown PV triggers Mole fetch, seed, subscribe, and succeeds."""
        samples = _make_samples()
        fetch_calls: list[tuple[str, int, int]] = []
        subscribed: list[list[str]] = []

        async def fake_fetch_window(pv, t0, t1, **kwargs):
            fetch_calls.append((pv, t0, t1))
            return samples

        async def fake_subscribe_live(pvs):
            subscribed.append(pvs)

        adapter = VoleDataSource(test_service, fake_fetch_window, fake_subscribe_live, clock=fake_clock)
        window = await adapter.read_aligned(["PV:NEW"], _BASE + 100, _BASE + 500, align="last", index="union")

        assert len(window.ts_ns) > 0
        assert "PV:NEW" in window.arrays
        assert len(fetch_calls) == 1
        assert fetch_calls[0][0] == "PV:NEW"
        assert subscribed == [["PV:NEW"]]

    async def test_read_aligned_does_not_reprovision(self, test_service, fake_clock):
        """Second read for the same PV skips Mole fetch."""
        samples = _make_samples()
        fetch_count = 0

        async def fake_fetch_window(pv, t0, t1, **kwargs):
            nonlocal fetch_count
            fetch_count += 1
            return samples

        async def fake_subscribe_live(pvs):
            pass

        adapter = VoleDataSource(test_service, fake_fetch_window, fake_subscribe_live, clock=fake_clock)
        await adapter.read_aligned(["PV:X"], _BASE + 100, _BASE + 500, align="last", index="union")
        await adapter.read_aligned(["PV:X"], _BASE + 100, _BASE + 500, align="last", index="union")

        assert fetch_count == 1

    async def test_read_aligned_provisions_only_missing_pvs(self, test_service, fake_clock):
        """Only PVs not already buffered are provisioned."""
        await test_service.seed("PV:EXISTS", _make_samples())

        fetched_pvs: list[str] = []

        async def fake_fetch_window(pv, t0, t1, **kwargs):
            fetched_pvs.append(pv)
            return _make_samples()

        async def fake_subscribe_live(pvs):
            pass

        adapter = VoleDataSource(test_service, fake_fetch_window, fake_subscribe_live, clock=fake_clock)
        window = await adapter.read_aligned(
            ["PV:EXISTS", "PV:MISSING"], _BASE + 100, _BASE + 500, align="last", index="union"
        )

        assert "PV:EXISTS" in window.arrays
        assert "PV:MISSING" in window.arrays
        assert fetched_pvs == ["PV:MISSING"]

    async def test_read_aligned_mole_error_still_subscribes_live(self, test_service, fake_clock):
        """If Mole fetch raises, the PV is still subscribed to live updates."""

        async def failing_fetch(pv, t0, t1, **kwargs):
            raise RuntimeError("archiver down")

        subscribed: list[list[str]] = []

        async def fake_subscribe_live(pvs):
            subscribed.append(pvs)

        adapter = VoleDataSource(test_service, failing_fetch, fake_subscribe_live, clock=fake_clock)

        with pytest.raises(PVNotBuffered):
            await adapter.read_aligned(["PV:FAIL"], _BASE + 100, _BASE + 500, align="last", index="union")

        assert subscribed == [["PV:FAIL"]]

    async def test_read_latest_provisions_missing_pv(self, test_service, fake_clock):
        """read_latest for an unknown PV triggers provisioning."""
        sample = Sample(ts_ns=_BASE + 500, value=99.0, severity=AlarmSeverity.NO_ALARM, source="archive")
        subscribed: list[list[str]] = []

        async def fake_fetch_window(pv, t0, t1, **kwargs):
            return [sample]

        async def fake_subscribe_live(pvs):
            subscribed.append(pvs)

        adapter = VoleDataSource(test_service, fake_fetch_window, fake_subscribe_live, clock=fake_clock)
        result = await adapter.read_latest("PV:LAZY")

        assert result is not None
        assert result.value == 99.0
        assert subscribed == [["PV:LAZY"]]

    async def test_read_latest_no_provision_without_callables(self, test_service):
        """Without provision callables, read_latest returns None for unknown PVs."""
        adapter = VoleDataSource(test_service)
        result = await adapter.read_latest("PV:NOPE")
        assert result is None

    async def test_read_aligned_no_provision_without_callables(self, test_service):
        """Without provision callables, read_aligned raises PVNotBuffered."""
        adapter = VoleDataSource(test_service)
        with pytest.raises(PVNotBuffered):
            await adapter.read_aligned(["PV:NOPE"], _BASE + 100, _BASE + 500, align="last", index="union")
