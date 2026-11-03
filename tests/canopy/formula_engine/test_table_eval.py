"""Live ``table`` evals through ``SubscriptionManager``: sharing, carry-over, emit, fan-out.

The manager runs against ``Harness`` (Vole's real ring behind the port plus a
scripted archive). The poll period is shortened to keep the tests quick except
where the server-derived interval itself is under test.
"""

from __future__ import annotations

import json
import asyncio
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from canopy.main import app as parent_app
from canopy.contracts.formula import FormulaSpec
from canopy.formula_engine.app import app as formula_engine_app
from canopy.formula_engine.config import FormulaEngineConfig
from canopy.formula_engine.errors import TableLimit, TableUpdateUnsupported
from canopy.formula_engine.dependencies import (
    get_formula_service,
    get_subscription_manager,
)
from tests.canopy.formula_engine.conftest import FakeDataSource
from canopy.formula_engine.schemas.formula import FormulaResultDTO
from canopy.formula_engine.services.parser import Parser
from tests.canopy.formula_engine.table_harness import (
    MS,
    T0,
    HOUR,
    S,
    Harness,
    timer_spec,
    strict_json,
    trigger_spec,
)
from canopy.formula_engine.services.table_store import TableSettings
from canopy.formula_engine.services.formula_service import FormulaService
from canopy.formula_engine.services.subscription_manager import SubscriptionManager

FIXTURES = Path(__file__).parent / "fixtures" / "table"
LOOKBACK = -30 * S


class Rig:
    """A manager wired to a harness, with recorded pins and live subscribes."""

    def __init__(self, h: Harness | None = None, **settings) -> None:
        self.h = h or Harness()
        self.pins: dict[str, list[str]] = {}
        self.subscribed: list[tuple[str, ...]] = []
        self.capacity: list[str] = []
        settings.setdefault("poll_ms", 20)
        self.port = self.h.port(
            pin=self._pin,
            unpin=self._unpin,
            ensure_capacity=lambda pv, max_points=None: self.capacity.append(pv),
        )
        self.mgr = SubscriptionManager(
            formula_service=FormulaService(FakeDataSource(), Parser(), FormulaEngineConfig()),
            on_change_fn=lambda pv, cb: lambda: None,
            subscribe_live_fn=self._subscribe_live,
            table_port=self.port,
            table_settings=TableSettings(**settings),
        )

    async def _subscribe_live(self, pvs: list[str]) -> None:
        self.subscribed.append(tuple(pvs))

    def _pin(self, pvs: list[str], holder: str) -> None:
        self.pins[holder] = list(pvs)

    async def _unpin(self, holder: str) -> list[str]:
        return self.pins.pop(holder, [])

    def pinned(self) -> set[str]:
        return {pv for pvs in self.pins.values() for pv in pvs}

    async def sub(self, client: str, sid: str, spec: FormulaSpec, collector: Collector, *, t0=LOOKBACK, interval=0):
        await self.mgr.add_subscription(client, sid, spec, t0, 0, collector.send, interval_ms=interval)

    def store_of(self, client: str, sid: str):
        return self.mgr._shared_evals[self.mgr._client_subs[client][sid]].store


class Collector:
    """Records what a listener was sent; frames are parsed on access, not in the send path."""

    def __init__(self, delay: asyncio.Event | None = None) -> None:
        self.raw: list[object] = []
        self._parsed: list[dict] = []
        self.delay = delay

    async def send(self, msg) -> None:
        if self.delay is not None:
            await self.delay.wait()
        self.raw.append(msg)

    @property
    def frames(self) -> list[dict]:
        for msg in self.raw[len(self._parsed) :]:
            self._parsed.append(strict_json(msg) if isinstance(msg, str) else msg)
        return self._parsed

    def results(self) -> list[dict]:
        return [f for f in self.frames if f["type"] == "result"]

    def errors(self) -> list[dict]:
        return [f for f in self.frames if f["type"] == "error"]

    def tables(self) -> list[dict]:
        return [f["data"]["table"] for f in self.results()]


async def wait_for(predicate, timeout: float = 3.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.005)


def seed_archive(h: Harness, pvs: list[str], value: float = 1.0) -> None:
    for pv in pvs:
        h.archived(pv, [(T0 - HOUR, value)])


# --------------------------------------------------------------------------- #
# Wiring
# --------------------------------------------------------------------------- #


class TestWiring:
    async def test_table_pins_synchronously_and_subscribes_its_inputs_live(self):
        rig = Rig()
        seed_archive(rig.h, ["X", "Y1"])
        c = Collector()
        await rig.sub("c", "s", timer_spec(), c)
        assert rig.pinned() == {"X", "Y1"}  # taken before the eval task ever ran
        await wait_for(lambda: c.results())
        assert rig.subscribed == [("X", "Y1")]
        assert sorted(rig.capacity) == ["X", "Y1"]
        await rig.mgr.shutdown()

    @pytest.mark.parametrize("client_interval", [0, 50])
    async def test_interval_is_server_derived(self, client_interval):
        rig = Rig(poll_ms=500)
        a, b = Collector(), Collector()
        await rig.sub("c1", "s1", trigger_spec(), a, interval=client_interval)
        await rig.sub("c2", "s2", trigger_spec(), b, interval=0)
        assert len(rig.mgr._shared_evals) == 1
        assert next(iter(rig.mgr._shared_evals.values())).interval_ms == 500
        await rig.mgr.shutdown()

    async def test_timer_interval_is_max_of_dt_and_500ms(self):
        rig = Rig(poll_ms=500)
        await rig.sub("c", "s", timer_spec(dt_ns=2 * S), Collector(), interval=50)
        assert next(iter(rig.mgr._shared_evals.values())).interval_ms == 2000
        await rig.mgr.shutdown()

    async def test_table_update_is_rejected(self):
        rig = Rig()
        await rig.sub("c", "s", timer_spec(), Collector())
        with pytest.raises(TableUpdateUnsupported):
            await rig.mgr.update_subscription("c", "s", Collector().send, t0_ns=-5 * S)
        await rig.mgr.shutdown()

    async def test_ninth_table_of_a_client_gets_table_limit(self):
        rig = Rig()
        for i in range(8):
            await rig.sub("c", f"s{i}", timer_spec(dt_ns=(i + 1) * S), Collector())
        with pytest.raises(TableLimit) as exc:
            await rig.sub("c", "s8", timer_spec(dt_ns=9 * S), Collector())
        assert exc.value.error_code.value == "table_limit"
        # Another client is unaffected.
        await rig.sub("other", "s0", timer_spec(dt_ns=9 * S), Collector())
        await rig.mgr.shutdown()

    async def test_scalar_eval_pins_its_inputs_for_the_whole_eval(self):
        rig = Rig()
        spec = FormulaSpec(kind="scalar", expression="{a} + 1", bindings={"a": "ca://PV:A"})
        await rig.mgr.add_subscription("c", "s", spec, 0, 0, Collector().send)
        assert rig.pinned() == {"PV:A"}
        await rig.mgr.remove_subscription("c", "s")
        await wait_for(lambda: not rig.pins)
        await rig.mgr.shutdown()


# --------------------------------------------------------------------------- #
# Sharing and carry-over
# --------------------------------------------------------------------------- #


class TestSharingAndCarryOver:
    async def test_two_concurrent_subscribes_during_a_slow_seed_share_one_eval_and_fetch(self):
        rig = Rig()
        seed_archive(rig.h, ["X", "Y1"])
        rig.h.gate = asyncio.Event()
        a, b = Collector(), Collector()
        await rig.sub("c1", "s1", timer_spec(), a)
        await asyncio.sleep(0.05)
        await rig.sub("c2", "s2", timer_spec(), b)
        assert len(rig.mgr._shared_evals) == 1
        rig.h.gate.set()
        await wait_for(lambda: a.results() and b.results())
        assert len(rig.h.mole_calls("X")) == 1
        assert len(rig.h.mole_calls("Y1")) == 1
        await rig.mgr.shutdown()

    @pytest.mark.parametrize("mid_seed", [False, True])
    async def test_role_edit_unsubscribe_then_subscribe_fetches_nothing_twice(self, mid_seed):
        rig = Rig()
        seed_archive(rig.h, ["X", "Y1", "Y2"])
        a, b = Collector(), Collector()
        if mid_seed:
            rig.h.gate = asyncio.Event()
        await rig.sub("c", "a", timer_spec(), a)
        if mid_seed:
            await asyncio.sleep(0.05)
        else:
            await wait_for(lambda: a.results())
        # The client's same-tick swap: unsubscribe old, subscribe new.
        await rig.mgr.remove_subscription("c", "a")
        await rig.sub("c", "b", timer_spec(y={"y1": "Y1", "y2": "Y2"}), b)
        if rig.h.gate is not None:
            rig.h.gate.set()
        await wait_for(lambda: b.results())
        assert len(rig.h.mole_calls("X")) == 1
        assert len(rig.h.mole_calls("Y1")) == 1
        assert len(rig.h.mole_calls("Y2")) == 1
        table = b.tables()[-1]
        assert set(table["columns"]) == {"x", "y1", "y2"}
        await rig.mgr.shutdown()

    async def test_two_quick_role_edits_during_a_running_seed_fetch_nothing_twice(self):
        rig = Rig()
        seed_archive(rig.h, ["X", "Y1", "Y2", "Y3"])
        rig.h.gate = asyncio.Event()
        c = Collector()
        await rig.sub("c", "a", timer_spec(), Collector())
        await asyncio.sleep(0.02)
        await rig.mgr.remove_subscription("c", "a")
        await rig.sub("c", "b", timer_spec(y={"y1": "Y1", "y2": "Y2"}), Collector())
        await asyncio.sleep(0.02)
        await rig.mgr.remove_subscription("c", "b")
        await rig.sub("c", "c", timer_spec(y={"y1": "Y1", "y2": "Y2", "y3": "Y3"}), c)
        rig.h.gate.set()
        await wait_for(lambda: c.results())
        for pv in ("X", "Y1", "Y2", "Y3"):
            assert len(rig.h.mole_calls(pv)) == 1, pv
        await rig.mgr.shutdown()

    async def test_swapping_y1_to_another_pv_reseeds_it_and_never_shows_the_old_cells(self):
        rig = Rig()
        rig.h.archived("X", [(T0 - HOUR, 1.0)])
        rig.h.archived("PA", [(T0 - HOUR, 111.0)])
        rig.h.archived("PB", [(T0 - HOUR, 222.0)])
        a, b = Collector(), Collector()
        await rig.sub("c", "a", timer_spec(y={"y1": "PA"}), a)
        await wait_for(lambda: a.results())
        await rig.mgr.remove_subscription("c", "a")
        await rig.sub("c", "b", timer_spec(y={"y1": "PB"}), b)
        await wait_for(lambda: b.results())
        assert len(rig.h.mole_calls("PB")) == 1
        assert len(rig.h.mole_calls("X")) == 1
        for table in b.tables():
            assert 111.0 not in table["columns"]["y1"]
        assert set(b.tables()[0]["columns"]["y1"]) == {222.0}
        await rig.mgr.shutdown()

    async def test_the_swap_leaves_an_unrelated_timer_tab_untouched(self):
        rig = Rig()
        for pv, v in (("X", 1.0), ("PA", 2.0), ("PB", 3.0), ("Q", 4.0)):
            rig.h.archived(pv, [(T0 - HOUR, v)])
        other, a, b = Collector(), Collector(), Collector()
        await rig.sub("c2", "o", timer_spec(y={"y1": "Q"}), other)
        await rig.sub("c1", "a", timer_spec(y={"y1": "PA"}), a)
        await wait_for(lambda: other.results() and a.results())
        other_store = rig.store_of("c2", "o")
        before_ts, before_vals = other_store.rows.ordered()
        before_view = other_store.view
        await rig.mgr.remove_subscription("c1", "a")
        await rig.sub("c1", "b", timer_spec(y={"y1": "PB"}), b)
        await wait_for(lambda: b.results())
        assert rig.store_of("c2", "o") is other_store
        assert other_store.view is before_view
        after_ts, after_vals = other_store.rows.ordered()
        assert after_ts[: len(before_ts)].tolist() == before_ts.tolist()
        assert set(after_vals) == set(before_vals) == {"X", "Q"}
        assert all(set(t["columns"]) == {"x", "y1"} and 2.0 not in t["columns"]["y1"] for t in other.tables())
        await rig.mgr.shutdown()

    async def test_raising_n_seeds_only_older_rows(self):
        rig = Rig()
        rig.h.archived("TRIG", [(T0 - 3000 * S + k * S, float(k)) for k in range(3000)])
        rig.h.archived("X", [(T0 - 2 * HOUR, 1.0)])
        rig.h.archived("Y1", [(T0 - 2 * HOUR, 2.0)])
        a, b = Collector(), Collector()
        await rig.sub("c", "a", trigger_spec(n=1000), a, t0=-HOUR)
        await wait_for(lambda: a.results())
        oldest = min(a.tables()[-1]["tsNs"])
        calls_before = len(rig.h.calls)
        await rig.mgr.remove_subscription("c", "a")
        await rig.sub("c", "b", trigger_spec(n=2000), b, t0=-HOUR)
        await wait_for(lambda: b.results())
        new_calls = rig.h.calls[calls_before:]
        assert new_calls, "older rows need the archive"
        assert all(t1 < oldest for _, _, _, t1 in new_calls)
        assert len(b.tables()[-1]["tsNs"]) == 2000
        await rig.mgr.shutdown()

    async def test_a_new_trigger_pv_seeds_from_scratch(self):
        rig = Rig()
        seed_archive(rig.h, ["X", "Y1"])
        rig.h.archived("TRIG", [(T0 - 10 * S, 1.0)])
        rig.h.archived("TRIG2", [(T0 - 10 * S, 1.0)])
        a, b = Collector(), Collector()
        await rig.sub("c", "a", trigger_spec(), a)
        await wait_for(lambda: a.results())
        store_a = rig.store_of("c", "a")
        await rig.mgr.remove_subscription("c", "a")
        await rig.sub("c", "b", trigger_spec(trig="TRIG2"), b)
        await wait_for(lambda: b.results())
        assert rig.store_of("c", "b") is not store_a
        assert len(rig.h.mole_calls("TRIG2")) == 1
        assert len(rig.h.mole_calls("X")) == 2  # a different index: X is seeded again
        await rig.mgr.shutdown()

    async def test_hide_for_30s_then_return_rebuilds_the_gap_from_the_rings(self):
        rig = Rig()
        seed_archive(rig.h, ["X", "Y1"])
        a, b = Collector(), Collector()
        await rig.sub("c", "a", timer_spec(), a, t0=-HOUR)
        await wait_for(lambda: a.results())
        calls = len(rig.h.calls)
        await rig.mgr.remove_subscription("c", "a")
        # 30 s hidden: the pinned rings keep ingesting.
        for k in range(30):
            rig.h.advance(S)
            await rig.h.live("X", rig.h.now, float(k))
        await rig.sub("c", "b", timer_spec(), b, t0=-HOUR)
        await wait_for(lambda: b.results())
        assert len(rig.h.calls) == calls  # no Mole call
        ts = b.tables()[0]["tsNs"]
        for k in range(1, 28):
            assert T0 + k * S in ts
        await rig.mgr.shutdown()

    async def test_a_65th_lingering_store_evicts_the_oldest_which_then_seeds_again(self):
        rig = Rig()
        seed_archive(rig.h, ["X", "Y1"])
        first = Collector()
        await rig.sub("c", "first", timer_spec(dt_ns=S), first)
        await wait_for(lambda: first.results())
        await rig.mgr.remove_subscription("c", "first")
        for i in range(64):
            c = Collector()
            await rig.sub("c", f"s{i}", timer_spec(dt_ns=(i + 2) * S), c)
            await wait_for(lambda c=c: c.results())
            await rig.mgr.remove_subscription("c", f"s{i}")
        assert len(rig.mgr._lingering) == 64
        x_calls = len(rig.h.mole_calls("X"))
        again = Collector()
        await rig.sub("c", "again", timer_spec(dt_ns=S), again)
        await wait_for(lambda: again.results())
        assert len(rig.h.mole_calls("X")) == x_calls + 1
        await rig.mgr.shutdown()

    async def test_a_lingering_store_expires_after_the_linger_time(self):
        rig = Rig()
        seed_archive(rig.h, ["X", "Y1"])
        c = Collector()
        await rig.sub("c", "s", timer_spec(), c)
        await wait_for(lambda: c.results())
        await rig.mgr.remove_subscription("c", "s")
        assert len(rig.mgr._lingering) == 1
        rig.h.advance(61 * S)
        rig.mgr._sweep_lingering()
        assert not rig.mgr._lingering
        await wait_for(lambda: not rig.pins)


# --------------------------------------------------------------------------- #
# Seed lifecycle and emit
# --------------------------------------------------------------------------- #


class TestEmit:
    async def test_slow_seed_sends_the_wait_frame_once_then_the_result(self):
        rig = Rig()
        seed_archive(rig.h, ["X", "Y1"])
        rig.h.gate = asyncio.Event()
        c = Collector()
        await rig.sub("c", "s", timer_spec(), c)
        await wait_for(lambda: c.errors())
        await asyncio.sleep(0.15)
        assert len(c.errors()) == 1
        err = c.errors()[0]
        assert err["retryable"] is True and err["id"] == "s"
        rig.h.gate.set()
        await wait_for(lambda: c.results())
        assert len(c.errors()) == 1
        await rig.mgr.shutdown()

    async def test_silent_trigger_gets_one_seed_done_result_then_nothing(self):
        rig = Rig()
        c = Collector()
        await rig.sub("c", "s", trigger_spec(), c)
        await wait_for(lambda: c.results())
        await asyncio.sleep(0.2)
        assert len(c.results()) == 1
        assert c.tables()[0]["totalRows"] == 0
        await rig.mgr.shutdown()

    async def test_single_trigger_sample_produces_exactly_one_more_frame(self):
        rig = Rig()
        await rig.h.live("X", T0, 1.0)
        c = Collector()
        await rig.sub("c", "s", trigger_spec(), c)
        await wait_for(lambda: c.results())
        rig.h.advance(100 * MS)
        await rig.h.live("TRIG", rig.h.now, 5.0)
        await wait_for(lambda: len(c.results()) == 2)
        for _ in range(10):  # the row finalizes meanwhile
            rig.h.advance(500 * MS)
            await asyncio.sleep(0.03)
        assert len(c.results()) == 2
        assert rig.store_of("c", "s").rows.count == 1
        await rig.mgr.shutdown()

    async def test_timer_emits_each_interval_with_a_non_null_row_only(self):
        rig = Rig()
        c = Collector()
        await rig.sub("c", "s", timer_spec(dt_ns=S), c)
        await wait_for(lambda: c.results())
        for _ in range(3):  # no data: all-null grid rows, nothing emitted
            rig.h.advance(S)
            await asyncio.sleep(0.05)
        assert len(c.results()) == 1
        await rig.h.live("X", rig.h.now, 1.0)
        for i in range(3):
            rig.h.advance(S)
            await wait_for(lambda i=i: len(c.results()) == 2 + i)
        await rig.mgr.shutdown()

    async def test_failed_seed_retry_patches_and_emits_once(self):
        from canopy.mole.adapters.base import ArchiverUnavailable

        rig = Rig(retry_ns=60 * S)
        seed_archive(rig.h, ["X"])
        rig.h.archived("Y1", [(T0 - HOUR, 9.0)])
        rig.h.errors["Y1"] = ArchiverUnavailable("down")
        c = Collector()
        await rig.sub("c", "s", timer_spec(), c)
        await wait_for(lambda: c.results())
        assert c.tables()[0]["inputs"]["y1"]["history"] == "live-only"
        del rig.h.errors["Y1"]
        n = len(c.results())
        rig.h.now += 61 * S  # receive clock moves, the watermark does not: no new rows
        await wait_for(lambda: len(c.results()) > n)
        await asyncio.sleep(0.1)
        assert len(c.results()) == n + 1
        table = c.tables()[-1]
        assert table["inputs"]["y1"]["history"] == "seeded"
        assert 9.0 in table["columns"]["y1"]
        calls = len(rig.h.mole_calls("Y1"))
        await asyncio.sleep(0.1)
        assert len(rig.h.mole_calls("Y1")) == calls  # a successful retry is not repeated
        assert rig.store_of("c", "s").due_retries(rig.h.now + 120 * S) == []
        await rig.mgr.shutdown()

    async def test_unreachable_binding_still_gets_its_seed_done_result(self):
        from canopy.mole.adapters.base import PVNotFoundInArchiver

        rig = Rig()
        seed_archive(rig.h, ["X"])
        rig.h.errors["Y1"] = PVNotFoundInArchiver("unknown")
        c = Collector()
        loop = asyncio.get_running_loop()
        start = loop.time()
        await rig.sub("c", "s", timer_spec(), c)
        await wait_for(lambda: c.results(), timeout=1.0)
        assert loop.time() - start < 1.0
        assert c.tables()[0]["inputs"]["y1"]["history"] == "not-archived"
        await rig.mgr.shutdown()


# --------------------------------------------------------------------------- #
# Fan-out (r4:M4-1)
# --------------------------------------------------------------------------- #


class TestFanOut:
    async def test_a_slow_listener_does_not_delay_another(self):
        rig = Rig()
        await rig.h.live("X", T0, 1.0)
        stuck = asyncio.Event()
        slow, fast = Collector(delay=stuck), Collector()
        await rig.sub("c1", "slow", trigger_spec(), slow)
        await rig.sub("c2", "fast", trigger_spec(), fast)
        for _ in range(4):
            rig.h.advance(S)
            await rig.h.live("TRIG", rig.h.now, 1.0)
            await asyncio.sleep(0.06)
        await wait_for(lambda: len(fast.results()) >= 4)
        assert slow.frames == []
        stuck.set()
        await wait_for(lambda: slow.results())
        await asyncio.sleep(0.05)
        # The slow socket got the newest frame, not every intermediate one.
        assert len(slow.results()) < len(fast.results())
        assert slow.tables()[-1]["tsNs"][-1] == fast.tables()[-1]["tsNs"][-1]
        await rig.mgr.shutdown()

    async def test_a_broadcast_racing_a_join_never_leaves_the_older_frame_last(self):
        rig = Rig()
        await rig.h.live("X", T0, 1.0)
        first = Collector()
        await rig.sub("c1", "a", timer_spec(), first)
        await wait_for(lambda: first.results())
        shared = next(iter(rig.mgr._shared_evals.values()))
        older = shared.last_frame
        joiner = Collector()
        await rig.sub("c2", "b", timer_spec(), joiner)  # replay of `older` placed in the slot
        rig.h.advance(S)
        shared.store.tick()
        await rig.mgr._emit_table(shared)  # a newer broadcast lands before the slot drains
        newer = shared.last_frame
        assert newer is not older
        await wait_for(lambda: joiner.results())
        await asyncio.sleep(0.05)
        assert joiner.raw[-1] == newer.for_id("b")
        await rig.mgr.shutdown()


# --------------------------------------------------------------------------- #
# Wire format over the WebSocket
# --------------------------------------------------------------------------- #


def _key_sets(obj, path="") -> dict[str, set]:
    out: dict[str, set] = {}
    if isinstance(obj, dict):
        out[path] = set(obj)
        for k, v in obj.items():
            sub = path + "/" + ("*" if path.endswith(("columns", "inputs")) else k)
            for p, keys in _key_sets(v, sub).items():
                out.setdefault(p, set()).update(keys)
    return out


@pytest.fixture
def ws_rig():
    h = Harness()
    h.archived("X", [(T0 - HOUR, 1.0)])
    h.archived("Y1", [(T0 - HOUR, float("nan"))])
    holder: dict = {}

    rig_box: list[Rig] = []

    def manager():
        if not rig_box:
            rig_box.append(Rig(h))
        return rig_box[0].mgr

    formula_service = FormulaService(FakeDataSource(), Parser(), FormulaEngineConfig())
    formula_engine_app.dependency_overrides[get_formula_service] = lambda: formula_service
    formula_engine_app.dependency_overrides[get_subscription_manager] = manager
    try:
        with TestClient(parent_app) as client:
            holder["client"] = client
            yield client, h
    finally:
        formula_engine_app.dependency_overrides.pop(get_formula_service, None)
        formula_engine_app.dependency_overrides.pop(get_subscription_manager, None)


WS_URL = "/formula-engine/v1/ws/formulas?api_key=test-read"


def _table_subscribe() -> dict:
    spec = timer_spec().model_dump(by_alias=True, exclude_none=True)
    return {"type": "subscribe", "spec": spec, "t0Ns": LOOKBACK}


class TestWebSocketWire:
    def test_every_frame_type_arrives_as_text_and_the_table_frame_matches_the_fixture(self, ws_rig):
        client, _ = ws_rig
        with client.websocket_connect(WS_URL) as ws:
            ws.send_json(_table_subscribe())
            subscribed = json.loads(ws.receive_text())
            assert subscribed["type"] == "subscribed"
            frame = None
            for _ in range(5):
                msg = json.loads(ws.receive_text())
                if msg["type"] == "result":
                    frame = msg
                    break
            assert frame is not None
            ws.send_json({"type": "ping"})
            while True:
                pong = json.loads(ws.receive_text())
                if pong["type"] == "pong":
                    break
            ws.send_json({"type": "update", "id": subscribed["id"], "spec": _table_subscribe()["spec"]})
            while True:
                err = json.loads(ws.receive_text())
                if err["type"] == "error":
                    break
            assert err["code"] == "table_update_unsupported" and err["retryable"] is False
            ws.send_json({"type": "unsubscribe", "id": subscribed["id"]})

        # Validates against the REST DTO and has the fixture's key sets at every level.
        FormulaResultDTO.model_validate(frame["data"])
        fixture = json.loads((FIXTURES / "result-trigger.json").read_text())
        got, want = _key_sets(frame), _key_sets(fixture)
        assert got[""] == want[""]
        assert got["/data"] == want["/data"]
        assert got["/data/table"] == want["/data/table"]
        assert got["/data/table/inputs/*"] == want["/data/table/inputs/*"]
        # NaN archived for y1 went out as null.
        assert frame["data"]["table"]["columns"]["y1"][0] is None


class TestWebSocketUnreachable:
    def test_one_unreachable_binding_gets_the_seed_done_result_and_a_pong_within_1s(self, ws_rig):
        import time

        from canopy.mole.adapters.base import PVNotFoundInArchiver

        client, h = ws_rig
        h.errors["Y1"] = PVNotFoundInArchiver("unknown to the archiver")
        with client.websocket_connect(WS_URL) as ws:
            start = time.monotonic()
            ws.send_json(_table_subscribe())
            ws.send_json({"type": "ping"})
            seen: dict[str, dict] = {}
            while not {"result", "pong"} <= set(seen):
                msg = json.loads(ws.receive_text())
                seen.setdefault(msg["type"], msg)
            assert time.monotonic() - start < 1.0
        assert seen["result"]["data"]["table"]["inputs"]["y1"]["history"] == "not-archived"


# --------------------------------------------------------------------------- #
# Through canopy_mcp's forward (text frames on every hop)
# --------------------------------------------------------------------------- #


class FormulaEngineServer:
    """The real ``/formula-engine/v1/ws/formulas`` endpoint over a table-capable manager, under uvicorn."""

    def __init__(self) -> None:
        import time
        import threading

        import uvicorn
        from fastapi import FastAPI

        from canopy.core.security import ws_require_read_access
        from canopy.formula_engine.api.v1 import ws as formula_engine_ws

        self.h = Harness()
        self.h.archived("X", [(T0 - HOUR, 1.0)])
        self.h.archived("Y1", [(T0 - HOUR, float("nan"))])
        self.rig: Rig | None = None
        app = FastAPI()
        app.include_router(formula_engine_ws.router, prefix="/formula-engine/v1")

        @app.on_event("startup")
        async def _start() -> None:
            self.rig = Rig(self.h)

        @app.on_event("shutdown")
        async def _stop() -> None:
            await self.rig.mgr.shutdown()

        app.dependency_overrides[get_subscription_manager] = lambda: self.rig.mgr
        app.dependency_overrides[ws_require_read_access] = lambda: None
        self._server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning", lifespan="on"))
        self._thread = threading.Thread(target=self._server.run, daemon=True)
        self._thread.start()
        deadline = time.monotonic() + 10
        while not self._server.started:
            if time.monotonic() > deadline:
                raise RuntimeError("formula engine server did not start")
            time.sleep(0.02)
        self.port = self._server.servers[0].sockets[0].getsockname()[1]

    def stop(self) -> None:
        self._server.should_exit = True
        self._thread.join(timeout=10)


@pytest.fixture
def formula_engine_server():
    server = FormulaEngineServer()
    yield server
    server.stop()


def test_table_frames_arrive_as_text_through_the_canopy_mcp_forward(tmp_path, formula_engine_server):
    pytest.importorskip("fcntl", reason="the panel server is POSIX-only: its draft store takes fcntl file locks")
    pytest.importorskip("canopy_mcp", reason="the panel server is a separate package")
    from canopy_mcp.app import create_app
    from canopy_mcp.config import Settings

    drafts = tmp_path / "drafts"
    drafts.mkdir()
    # The test client names its server "testserver" in Host, which the relay must accept.
    settings = Settings(
        base_url=f"http://127.0.0.1:{formula_engine_server.port}",
        api_key="k",
        drafts_dir=drafts,
        self_url="http://testserver",
    )
    with TestClient(create_app(settings, startup_hooks=[])) as client:
        with client.websocket_connect("/formula-engine/v1/ws/formulas") as ws:
            ws.send_json(_table_subscribe())
            seen: dict[str, dict] = {}
            while "result" not in seen:
                msg = json.loads(ws.receive_text())  # receive_text fails on a binary frame
                seen.setdefault(msg["type"], msg)
            ws.send_json({"type": "ping"})
            while "pong" not in seen:
                msg = json.loads(ws.receive_text())
                seen.setdefault(msg["type"], msg)
    assert {"subscribed", "result", "pong"} <= set(seen)
    data = seen["result"]["data"]
    FormulaResultDTO.model_validate(data)
    assert set(data) == {"kind", "table"}
    assert data["table"]["columns"]["y1"][0] is None
