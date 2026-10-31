"""The budgeted, off-loop archive fetch for Vole seeds.

The appliance adapter: windows walk newest-first, each sized from the previous
window's observed rate to at most ``SEED_SAMPLE_BUDGET`` samples (never doubled),
every response is parsed to numpy arrays in a worker thread (the patched parser
asserts it). The archiver service: ``fetch_last_before`` refuses more than 500
timestamps and runs at most 8 lookups at once, terminal errors propagate
unchanged, and ``event_faithful`` tells a polled archive from an event archive.
"""

from __future__ import annotations

import re
import asyncio
import threading
from datetime import datetime
from urllib.parse import parse_qs, urlsplit

import numpy as np
import pytest
from pytest_httpx import HTTPXMock

from canopy.mole import gateway
from canopy.mole.config import MoleConfig
from canopy.mole.adapters import appliance as appliance_module
from canopy.contracts.sample import Sample, AlarmSeverity
from canopy.mole.adapters.base import (
    WINDOW_GROWTH_MAX,
    SEED_SAMPLE_BUDGET,
    LAST_BEFORE_CONCURRENCY,
    LAST_BEFORE_MAX_TIMESTAMPS,
    ArchiveBlock,
    PVNotFoundInArchiver,
    ArchiverNotConfigured,
    fetch_window_budgeted_via,
)
from canopy.mole.adapters.registry import AdapterRegistry
from canopy.mole.adapters.appliance import EpicsArchiverApplianceAdapter
from tests.canopy.mole.adapters._fakes import FakeArchiverAdapter
from canopy.mole.services.archiver_service import ArchiverService

S = 1_000_000_000
T1 = 1_790_769_600 * S  # 2026-09-30T12:00:00Z
GETDATA = re.compile(r".*/retrieval/data/getData\.json.*")


def _iso_to_ns(value: str) -> int:
    return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000) * 1_000_000


def _window_of(request) -> tuple[int, int]:
    q = parse_qs(urlsplit(str(request.url)).query)
    return _iso_to_ns(q["from"][0]), _iso_to_ns(q["to"][0])


def _rows(t0_ns: int, t1_ns: int, rate_hz: float) -> list[dict]:
    step = int(S / rate_hz)
    first = -(-t0_ns // step) * step
    return [
        {"secs": ts // S, "nanos": ts % S, "val": float(i), "severity": i % 4}
        for i, ts in enumerate(range(first, t1_ns + 1, step))
    ]


@pytest.fixture
async def appliance():
    a = EpicsArchiverApplianceAdapter(name="test", url="https://archappl.example", timeout_s=5.0)
    yield a
    await a.close()


@pytest.fixture
def off_loop_parsers(monkeypatch):
    """Patch the appliance parser to record the thread it ran on."""
    threads: list[threading.Thread] = []

    def wrap(fn):
        def wrapped(*args, **kwargs):
            threads.append(threading.current_thread())
            return fn(*args, **kwargs)

        return wrapped

    monkeypatch.setattr(appliance_module, "_parse_raw_arrays", wrap(appliance_module._parse_raw_arrays))
    return threads


# --- Appliance ------------------------------------------------------------------ #


class TestApplianceBudgeted:
    async def test_parses_off_loop_and_returns_ascending_arrays(
        self, appliance, httpx_mock: HTTPXMock, off_loop_parsers
    ):
        def respond(request):
            lo, hi = _window_of(request)
            rows = _rows(lo, hi, 1.0) if lo != hi else [{"secs": lo // S - 7, "nanos": 0, "val": -1.0, "severity": 0}]
            return appliance_module.httpx.Response(200, json=[{"meta": {"name": "P"}, "data": rows}])

        httpx_mock.add_callback(respond, url=GETDATA, is_reusable=True)
        block = await appliance.fetch_window_budgeted("P", T1 - 120 * S, T1)

        assert isinstance(block, ArchiveBlock)
        assert off_loop_parsers and all(t is not threading.main_thread() for t in off_loop_parsers)
        assert block.ts_ns.dtype == np.int64 and block.values.dtype == object and block.severities.dtype == np.int8
        assert np.all(np.diff(block.ts_ns) > 0)
        assert block.ts_ns[0] == T1 - 127 * S  # the last sample before the oldest window, at its true ts
        assert block.ts_ns[-1] == T1 and len(block) == 122
        assert block.severities.tolist()[1:5] == [0, 1, 2, 3]
        assert block.values[1] == 0.0

    async def test_windows_walk_newest_first_sized_by_observed_rate(self, appliance, httpx_mock: HTTPXMock):
        windows: list[tuple[int, int]] = []
        step_at = T1 - 200 * S  # 10x the rate before this instant

        def respond(request):
            lo, hi = _window_of(request)
            if lo == hi:
                return appliance_module.httpx.Response(200, json=[{"meta": {}, "data": []}])
            windows.append((lo, hi))
            rate = 10.0 if hi <= step_at else 1.0
            return appliance_module.httpx.Response(200, json=[{"meta": {}, "data": _rows(lo, hi, rate)}])

        httpx_mock.add_callback(respond, url=GETDATA, is_reusable=True)
        await appliance.fetch_window_budgeted("P", T1 - 3600 * S, T1, budget=100)

        widths = [(hi - lo) // S for lo, hi in windows]
        assert widths[0] == 60  # the first window is 60 s
        # Newest first, contiguous (the wire is ms-precise, so adjacent windows meet within a ms).
        assert windows[0][1] == T1 and all(windows[i][0] >= windows[i + 1][1] for i in range(len(windows) - 1))
        assert 95 <= widths[1] <= 100  # 61 samples over 60 s at 1 Hz: budget / rate
        # A window over 2x the budget (10 Hz) shrinks the next one proportionally, never doubling.
        over = next(i for i, (lo, hi) in enumerate(windows) if hi <= step_at)
        assert widths[over + 1] <= widths[over] // 5
        assert windows[-1][0] == T1 - 3600 * S  # covers the whole lookback

    async def test_quiet_newest_window_cannot_balloon_the_next_request(self):
        """An empty recent window grows the next one at most WINDOW_GROWTH_MAX-fold, not by budget/1."""
        quiet_from = T1 - 60 * S  # nothing in the newest 60 s, 10 Hz before it
        requests: list[tuple[int, int, int]] = []

        async def fetch_block(pv, lo, hi, include_last_before):
            start = max(lo, T1 - 3600 * S)
            first = -(-start // (S // 10)) * (S // 10)
            ts = np.arange(first, min(hi, quiet_from - 1) + 1, S // 10, dtype=np.int64)
            requests.append((lo, hi, len(ts)))
            return ArchiveBlock(ts, np.full(len(ts), 1.0, dtype=object), np.zeros(len(ts), dtype=np.int8))

        budget = 100
        block = await fetch_window_budgeted_via(fetch_block, "P", T1 - 3600 * S, T1, budget=budget)

        assert requests[0][2] == 0  # the quiet newest window
        widths = [hi - lo + 1 for lo, hi, _ in requests]
        assert all(widths[i + 1] <= widths[i] * WINDOW_GROWTH_MAX for i in range(len(widths) - 1))
        # Uncapped, the second request would span 6000 s (~60k samples at 10 Hz).
        assert requests[1][2] <= WINDOW_GROWTH_MAX * 60 * 10 + 1
        assert max(n for _, _, n in requests) <= WINDOW_GROWTH_MAX * 60 * 10 + 1
        assert requests[-1][0] == T1 - 3600 * S and len(block) == 35400

    async def test_pv_not_found_propagates_unchanged(self, appliance, httpx_mock: HTTPXMock):
        httpx_mock.add_response(url=GETDATA, status_code=400)
        with pytest.raises(PVNotFoundInArchiver):
            await appliance.fetch_window_budgeted("NOPE", T1 - 60 * S, T1)

    def test_event_faithful(self):
        assert EpicsArchiverApplianceAdapter.event_faithful is True


# --- service / gateway ------------------------------------------------------------ #


class _SlowFake(FakeArchiverAdapter):
    def __init__(self) -> None:
        super().__init__(name="slow")
        self.in_flight = 0
        self.peak = 0

    async def fetch_last_before(self, pv: str, ts_ns: int) -> Sample | None:
        self.in_flight += 1
        self.peak = max(self.peak, self.in_flight)
        try:
            await asyncio.sleep(0.005)
            return Sample(ts_ns=ts_ns - 1, value=1.0, severity=AlarmSeverity.NO_ALARM, source="archive")
        finally:
            self.in_flight -= 1


@pytest.fixture
def slow_service():
    adapter = _SlowFake()
    registry = AdapterRegistry({"slow": adapter}, default="slow")
    return adapter, ArchiverService(registry, MoleConfig(default_archiver="slow"))


class TestLastBefore:
    async def test_rejects_501_timestamps(self, slow_service):
        _, service = slow_service
        with pytest.raises(ValueError):
            await service.fetch_last_before("PV:TEST", list(range(LAST_BEFORE_MAX_TIMESTAMPS + 1)))

    async def test_runs_at_most_eight_lookups_at_once(self, slow_service):
        adapter, service = slow_service
        ts = [T1 - i * S for i in range(LAST_BEFORE_MAX_TIMESTAMPS)]
        got = await service.fetch_last_before("PV:TEST", ts)
        assert [s.ts_ns for s in got] == [t - 1 for t in ts]
        assert 1 < adapter.peak <= LAST_BEFORE_CONCURRENCY


class TestServiceAndGateway:
    async def test_not_configured_propagates_unchanged(self):
        service = ArchiverService(AdapterRegistry({}, default=""), MoleConfig())
        with pytest.raises(ArchiverNotConfigured):
            await service.fetch_window_budgeted("P", T1 - 60 * S, T1)
        with pytest.raises(ArchiverNotConfigured):
            await service.fetch_last_before("P", [T1])

    async def test_event_faithful_and_fallback_for_an_adapter_without_the_budgeted_fetch(self):
        adapter = FakeArchiverAdapter(name="fake")
        service = ArchiverService(
            AdapterRegistry({"fake": adapter}, default="fake"), MoleConfig(default_archiver="fake")
        )
        gateway.bind(service)
        try:
            assert await gateway.event_faithful() is True
            block = await gateway.fetch_window_budgeted("PV:TEST", T1 - 60 * S, T1)
            assert isinstance(block, ArchiveBlock) and len(block) > 0
            with pytest.raises(PVNotFoundInArchiver):
                await gateway.fetch_window_budgeted("NOPE", T1 - 60 * S, T1)
            assert len(await gateway.fetch_last_before("PV:TEST", [T1, T1 - S])) == 2
        finally:
            gateway.bind(None)

    def test_budget_constant(self):
        assert SEED_SAMPLE_BUDGET == 20_000
