"""Performance bounds of live ``table`` evals.

* Seed 9 inputs (X + 8 Y) x 36k archived samples plus a separate trigger to
  10 000 rows, then run 3 concurrent evals ticking for 10 s: per-eval tick CPU
  (thread time) p95 < 50 ms, event-loop lag p99 rises < 20 ms over an idle
  baseline measured in the same test, every encode runs off the loop and once
  per broadcast, each frame <= 2.5 MB, each store <= 1 MB.
* With the 9 rings already holding 36k samples each (Trace open), the seed
  keeps event-loop lag p99 < 20 ms.
"""

from __future__ import annotations

import time
import asyncio
import threading

import numpy as np
import pytest

from canopy.contracts.sample import Sample, AlarmSeverity
from canopy.mole.adapters.base import ArchiveBlock
from canopy.formula_engine.services import table_store as ts_mod
from canopy.formula_engine.services import subscription_manager as sm_mod
from tests.canopy.formula_engine.table_harness import (
    MS,
    T0,
    HOUR,
    Harness,
    trigger_spec,
)
from tests.canopy.formula_engine.test_table_eval import Rig, Collector

# The bounds are for the hardware CANOPY is operated on. A shared 2-core CI runner
# measures 50-90 ms where they allow 20, so those jobs deselect the marker and
# the gate on our own hosts enforces it.
pytestmark = pytest.mark.perf

INPUTS = ["X"] + [f"Y{i}" for i in range(1, 9)]
TRIGGERS = ["TRIG_A", "TRIG_B", "TRIG_C"]
SAMPLES = 36_000
N = 10_000


class FastArchive:
    """Precomputed archive arrays, sliced with searchsorted (the fake must not cost loop time)."""

    def __init__(self, h: Harness) -> None:
        self.blocks: dict[str, ArchiveBlock] = {}
        self.calls = 0
        step = HOUR // SAMPLES  # 10 Hz over the hour
        ts = T0 - HOUR + np.arange(SAMPLES, dtype=np.int64) * step
        for k, pv in enumerate(INPUTS):
            vals = np.empty(SAMPLES, dtype=object)
            vals[:] = np.sin(np.arange(SAMPLES) / 100.0 + k).tolist()  # Python floats, as Vole stores them
            self.blocks[pv] = ArchiveBlock(ts, vals, np.zeros(SAMPLES, np.int8))
        trig_n = 12_000  # 3.3 Hz: > N rows within the hour
        tts = T0 - HOUR + np.arange(trig_n, dtype=np.int64) * (HOUR // trig_n)
        for pv in TRIGGERS:
            vals = np.empty(trig_n, dtype=object)
            vals[:] = np.arange(trig_n, dtype=float).tolist()
            self.blocks[pv] = ArchiveBlock(tts, vals, np.zeros(trig_n, np.int8))

    async def fetch(self, pv: str, t0: int, t1: int, *, budget: int | None = None) -> ArchiveBlock:
        self.calls += 1
        b = self.blocks.get(pv)
        if b is None:
            return ArchiveBlock.empty()
        lo = max(int(np.searchsorted(b.ts_ns, t0, side="left")) - 1, 0)
        hi = int(np.searchsorted(b.ts_ns, t1, side="right"))
        return ArchiveBlock(b.ts_ns[lo:hi], b.values[lo:hi], b.severities[lo:hi])


class LagMonitor:
    def __init__(self, period: float = 0.005) -> None:
        self.period = period
        self.samples: list[float] = []
        self._task: asyncio.Task | None = None

    async def _run(self) -> None:
        while True:
            t = time.perf_counter()
            await asyncio.sleep(self.period)
            self.samples.append(time.perf_counter() - t - self.period)

    def start(self) -> None:
        self.samples = []
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> np.ndarray:
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        return np.asarray(self.samples)


def _perf_rig() -> tuple[Rig, FastArchive]:
    h = Harness()
    archive = FastArchive(h)
    rig = Rig(h, poll_ms=500)
    rig.port.fetch_window_budgeted = archive.fetch
    return rig, archive


async def _drive(rig: Rig, stop: asyncio.Event, pvs: list[str]) -> None:
    """10 Hz live updates on every PV; the receive clock and the watermark follow."""
    k = 0
    while not stop.is_set():
        await asyncio.sleep(0.1)
        rig.h.advance(100 * MS)
        for pv in pvs:
            await rig.h.live(pv, rig.h.now - 20 * MS, float(k % 17))
        k += 1


def _spec(trigger: str):
    ys = {f"y{i}": f"Y{i}" for i in range(1, 9)}
    return trigger_spec(n=N, y=ys, trig=trigger)


async def test_three_concurrent_10000_by_9_evals(monkeypatch):
    rig, archive = _perf_rig()
    main_thread = threading.get_ident()

    tick_cpu: list[float] = []
    real_tick = ts_mod.TableStore.tick

    def timed_tick(self, now=None):
        t = time.thread_time()
        try:
            return real_tick(self, now)
        finally:
            tick_cpu.append(time.thread_time() - t)

    frame_cpu: list[float] = []
    real_frame = ts_mod.TableStore.frame

    def timed_frame(self, now=None):
        t = time.thread_time()
        try:
            return real_frame(self, now)
        finally:
            frame_cpu.append(time.thread_time() - t)

    encode_threads: list[int] = []
    real_encode = sm_mod.encode_table_data

    def tracked_encode(frame):
        encode_threads.append(threading.get_ident())
        return real_encode(frame)

    posts = {"results": 0}
    real_post = rig.mgr._post

    def counted_post(shared, frame):
        if frame.head.get("type") == "result":
            posts["results"] += 1
        return real_post(shared, frame)

    monkeypatch.setattr(ts_mod.TableStore, "tick", timed_tick)
    monkeypatch.setattr(ts_mod.TableStore, "frame", timed_frame)
    monkeypatch.setattr(sm_mod, "encode_table_data", tracked_encode)
    monkeypatch.setattr(rig.mgr, "_post", counted_post)

    stop = asyncio.Event()
    driver = asyncio.create_task(_drive(rig, stop, INPUTS + TRIGGERS))
    monitor = LagMonitor()

    # Idle baseline, same process, same live ingest.
    monitor.start()
    await asyncio.sleep(2.0)
    idle = await monitor.stop()

    collectors = [Collector() for _ in TRIGGERS]
    for i, (trig, c) in enumerate(zip(TRIGGERS, collectors)):
        await rig.sub(f"c{i}", "s", _spec(trig), c, t0=-HOUR)
    deadline = asyncio.get_running_loop().time() + 60
    while not all(c.raw for c in collectors) or not all(c.results() for c in collectors):
        assert asyncio.get_running_loop().time() < deadline, "seeds did not finish"
        await asyncio.sleep(0.05)
    for c in collectors:
        assert len(c.tables()[0]["tsNs"]) == N

    tick_cpu.clear()
    frame_cpu.clear()
    monitor.start()
    await asyncio.sleep(10.0)
    busy = await monitor.stop()
    stop.set()
    await driver

    per_tick = np.asarray(tick_cpu)
    assert per_tick.size >= 3 * 15
    assert np.percentile(per_tick, 95) < 0.050, f"tick p95 {np.percentile(per_tick, 95) * 1e3:.1f} ms"
    assert np.percentile(np.asarray(frame_cpu), 95) < 0.050
    lag_delta = np.percentile(busy, 99) - np.percentile(idle, 99)
    assert lag_delta < 0.020, f"lag p99 delta {lag_delta * 1e3:.1f} ms"
    assert encode_threads and main_thread not in encode_threads  # encode off the loop
    # An encode is counted when its worker thread starts and posted after the await
    # returns, so one may still be in flight here: let it post before comparing.
    settle = asyncio.get_running_loop().time() + 2.0
    while len(encode_threads) != posts["results"] and asyncio.get_running_loop().time() < settle:
        await asyncio.sleep(0.01)
    assert len(encode_threads) == posts["results"]  # one encode per broadcast
    for c in collectors:
        assert max(len(r) for r in c.raw if isinstance(r, str)) <= 2_500_000
    for shared in rig.mgr._shared_evals.values():
        assert shared.store.nbytes() <= 1_000_000
    await rig.mgr.shutdown()


async def test_seed_from_rings_trace_already_holds_keeps_the_loop_responsive():
    rig, archive = _perf_rig()
    # Trace is open: the 9 rings (and the trigger's) already hold 36k samples each.
    for pv, block in archive.blocks.items():
        if pv not in INPUTS and pv != "TRIG_A":
            continue
        samples = [
            Sample.model_construct(ts_ns=int(t), value=v, severity=AlarmSeverity.NO_ALARM, source="archive")
            for t, v in zip(block.ts_ns.tolist(), block.values.tolist())
        ]
        await rig.h.buffer.seed(pv, samples)

    monitor = LagMonitor()
    monitor.start()
    await asyncio.sleep(1.0)
    idle = await monitor.stop()

    c = Collector()
    monitor.start()
    await rig.sub("c", "s", _spec("TRIG_A"), c, t0=-HOUR)
    deadline = asyncio.get_running_loop().time() + 60
    while not any(isinstance(m, str) and m.startswith('{"type":"result"') for m in c.raw):
        assert asyncio.get_running_loop().time() < deadline, "seed did not finish"
        await asyncio.sleep(0.01)
    during = await monitor.stop()

    assert archive.calls == 0  # the rings covered the whole window
    assert len(c.tables()[0]["tsNs"]) == N
    assert during.size > 0
    assert np.percentile(during, 99) < 0.020 + max(np.percentile(idle, 99), 0.0), (
        f"lag p99 during seed {np.percentile(during, 99) * 1e3:.1f} ms"
    )
    await rig.mgr.shutdown()


@pytest.mark.parametrize("n_inputs", [9])
async def test_store_is_at_most_1mb_at_10000_rows(n_inputs):
    rig, _ = _perf_rig()
    c = Collector()
    await rig.sub("c", "s", _spec("TRIG_A"), c, t0=-HOUR)
    deadline = asyncio.get_running_loop().time() + 60
    while not c.results():
        assert asyncio.get_running_loop().time() < deadline
        await asyncio.sleep(0.05)
    store = rig.store_of("c", "s")
    assert store.rows.count + len(store.tail_ts) >= N - 1
    assert store.nbytes() <= 1_000_000
    await rig.mgr.shutdown()
