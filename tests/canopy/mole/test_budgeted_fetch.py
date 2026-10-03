"""The budgeted, off-loop archive fetch for Vole seeds (b3:CF-2, b4:IA-5, b5:M-1, b6:OC-1, b6:M-3, b7:CC-2).

Both archiver adapters: windows walk newest-first, each sized from the previous
window's observed rate to at most ``SEED_SAMPLE_BUDGET`` samples (never doubled),
every response is parsed to numpy arrays in a worker thread (the patched parser
asserts it), ``osprey_mongodb`` halves the window on a ``max_raw`` overflow and
keeps the event loop's thread pool free while its cursor waits on the network,
``fetch_last_before`` refuses more than 500 timestamps and runs at most 8 lookups
at once, terminal errors propagate unchanged, and ``event_faithful`` tells a polled
archive from an event archive.
"""

from __future__ import annotations

import re
import json
import time
import asyncio
import threading
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlsplit

import bson
import numpy as np
import pytest
from pytest_httpx import HTTPXMock

from canopy.mole import gateway
from canopy.core.errors import BadRequest
from canopy.mole.config import MoleConfig
from canopy.contracts.sample import Sample, AlarmSeverity
from canopy.mole.adapters import appliance as appliance_module
from canopy.mole.adapters import osprey_mongodb as osprey_module
from canopy.mole.adapters.base import (
    LAST_BEFORE_CONCURRENCY,
    LAST_BEFORE_MAX_TIMESTAMPS,
    SEED_SAMPLE_BUDGET,
    WINDOW_GROWTH_MAX,
    ArchiveBlock,
    ArchiverNotConfigured,
    PVNotFoundInArchiver,
    fetch_window_budgeted_via,
)
from canopy.mole.adapters.registry import AdapterRegistry
from canopy.mole.adapters.appliance import EpicsArchiverApplianceAdapter
from canopy.mole.adapters.osprey_mongodb import DATE_FIELD, OspreyMongoRecorderAdapter
from canopy.mole.services.archiver_service import ArchiverService
from tests.canopy.mole.adapters._fakes import FakeArchiverAdapter

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
    """Patch both parsers to record the thread they ran on."""
    threads: list[threading.Thread] = []

    def wrap(fn):
        def wrapped(*args, **kwargs):
            threads.append(threading.current_thread())
            return fn(*args, **kwargs)

        return wrapped

    monkeypatch.setattr(appliance_module, "_parse_raw_arrays", wrap(appliance_module._parse_raw_arrays))
    monkeypatch.setattr(osprey_module, "_decode_raw_batches", wrap(osprey_module._decode_raw_batches))
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
        assert OspreyMongoRecorderAdapter.event_faithful is False


# --- OSPREY MongoDB (fake raw-batch collection, no server) ------------------------ #


class _FakeRawCursor:
    def __init__(self, docs: list[dict], delay_s: float) -> None:
        self._docs = docs
        self._delay_s = delay_s
        self._direction = 1
        self._limit: int | None = None

    def sort(self, field: str, direction: int):
        self._direction = direction
        return self

    def limit(self, n: int):
        self._limit = n
        return self

    async def __aiter__(self):
        docs = sorted(self._docs, key=lambda d: d[DATE_FIELD], reverse=self._direction < 0)
        if self._limit is not None:
            docs = docs[: self._limit]
        for i in range(0, len(docs), 101):
            if self._delay_s:
                await asyncio.sleep(self._delay_s)
            yield b"".join(bson.encode(d) for d in docs[i : i + 101])


class _FakeFindCursor:
    """The ``find().sort().limit().to_list()`` chain the unbudgeted ``fetch_window`` uses."""

    def __init__(self, docs: list[dict]) -> None:
        self.docs = docs

    def sort(self, field: str, direction: int):
        self.docs = sorted(self.docs, key=lambda d: d[field], reverse=direction < 0)
        return self

    def limit(self, n: int):
        self.docs = self.docs[:n]
        return self

    async def to_list(self, length=None):
        return list(self.docs)


class _FakeCollection:
    """``find_raw_batches`` over an in-memory recorder collection; ``find``/``find_one`` for the index."""

    def __init__(self, docs: list[dict], *, delay_s: float = 0.0) -> None:
        self.docs = docs
        self.delay_s = delay_s
        self.queries: list[dict] = []

    def _match(self, query: dict) -> list[dict]:
        out = []
        for d in self.docs:
            ok = True
            for key, cond in query.items():
                if "$exists" in cond:
                    ok &= (key in d) == cond["$exists"]
                elif key == DATE_FIELD:
                    v = d[DATE_FIELD]
                    ok &= all(
                        (op == "$gte" and v >= bound) or (op == "$lte" and v <= bound) or (op == "$lt" and v < bound)
                        for op, bound in cond.items()
                    )
            if ok:
                out.append(d)
        return out

    def find_raw_batches(self, query: dict, projection: dict | None = None):
        self.queries.append(query)
        return _FakeRawCursor(self._match(query), self.delay_s)

    def find(self, query: dict, projection: dict | None = None):
        self.queries.append(query)
        return _FakeFindCursor(self._match(query))

    async def find_one(self, query: dict, sort=None):
        docs = sorted(self._match(query), key=lambda d: d[DATE_FIELD], reverse=sort[0][1] < 0)
        return docs[0] if docs else None


def _recorder_docs(count: int, *, end_ns: int, step_s: int = 10, pv: str = "P") -> list[dict]:
    end = datetime.fromtimestamp(end_ns / S, tz=UTC)
    return [{DATE_FIELD: end - timedelta(seconds=step_s * i), pv: float(i), "expireAt": end} for i in range(count)]


def _osprey(coll: _FakeCollection, **caps) -> OspreyMongoRecorderAdapter:
    adapter = OspreyMongoRecorderAdapter(
        name="osprey",
        url="mongodb://u@h/db",
        password="x",
        database="db",
        collection="c",
        caps={**osprey_module.DEFAULT_CAPS, **caps},
    )
    adapter._collection = lambda: coll  # type: ignore[method-assign]
    return adapter


class TestOspreyBudgeted:
    async def test_decodes_off_loop_with_raw_batches(self, off_loop_parsers):
        coll = _FakeCollection(_recorder_docs(100, end_ns=T1))
        block = await _osprey(coll).fetch_window_budgeted("P", T1 - 600 * S, T1)
        assert off_loop_parsers and all(t is not threading.main_thread() for t in off_loop_parsers)
        assert len(block) == 62  # 61 in the window plus the last one before it
        assert np.all(np.diff(block.ts_ns) > 0) and block.ts_ns[-1] == T1
        assert block.values[-1] == 0.0 and set(block.severities.tolist()) == {0}

    async def test_max_raw_overflow_halves_the_window_and_retries(self):
        coll = _FakeCollection(_recorder_docs(400, end_ns=T1, step_s=1))
        block = await _osprey(coll, max_raw=20).fetch_window_budgeted("P", T1 - 59 * S, T1, budget=1000)
        assert len(block) == 61
        widths = [q[DATE_FIELD]["$lte"] - q[DATE_FIELD]["$gte"] for q in coll.queries if "$lte" in q[DATE_FIELD]]
        assert widths[0] == timedelta(seconds=59)
        assert widths[1] <= timedelta(seconds=30)  # halved after the overflow, never a BadRequest

    async def test_frame_encode_during_a_slow_seed_stays_fast(self):
        """The cursor waits on the loop, not in the thread pool a frame encode needs (b6:OC-1)."""
        coll = _FakeCollection(_recorder_docs(300, end_ns=T1, step_s=1), delay_s=0.3)
        seed = asyncio.create_task(_osprey(coll).fetch_window_budgeted("P", T1 - 299 * S, T1))
        await asyncio.sleep(0.05)
        t0 = time.monotonic()
        await asyncio.to_thread(json.dumps, {"type": "diff", "data": {"P": [1.0] * 1000}})
        assert time.monotonic() - t0 < 0.05
        block = await seed
        assert len(block) == 300

    async def test_pv_not_found_propagates_unchanged(self):
        with pytest.raises(PVNotFoundInArchiver):
            await _osprey(_FakeCollection(_recorder_docs(3, end_ns=T1))).fetch_window_budgeted("Q", T1 - 60 * S, T1)

    async def test_fetch_window_still_rejects_max_raw_overflow(self):
        coll = _FakeCollection(_recorder_docs(400, end_ns=T1, step_s=1))
        with pytest.raises(BadRequest):
            await _osprey(coll, max_raw=20).fetch_window("P", T1 - 59 * S, T1)


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
