"""The live ``table`` row store: seed, tick, finalize, late data, frames.

The store reads through the table port, exercised here through
``VoleDataSource`` over Vole's real ``BufferService`` (``table_harness``); the
archive is scripted.
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from canopy.contracts.sample import AlarmSeverity
from canopy.contracts.formula import FormulaSpec
from canopy.mole.adapters.base import (
    ArchiveBlock,
    ArchiverUnavailable,
    PVNotFoundInArchiver,
)
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
from canopy.formula_engine.services.table_store import (
    TableStore,
    TableSettings,
    build_view,
    encode_table_data,
)


def make_view(spec: FormulaSpec, lookback_ns: int = HOUR):
    parser = Parser()
    parsed = {}
    for name, expr in spec.columns.items():
        from canopy.formula_engine.services.evaluator import bare_binding

        if bare_binding(expr, spec.bindings) is not None:
            parsed[name] = None
        else:
            tokens = parser.extract_tokens(expr)
            parsed[name] = parser.parse(expr, {t: spec.bindings[t] for t in tokens})
    return build_view(spec, parsed, lookback_ns)


async def seeded_store(h: Harness, spec: FormulaSpec, lookback_ns: int = HOUR, settings: TableSettings | None = None):
    store = TableStore(make_view(spec, lookback_ns), settings or TableSettings(), h.port())
    store.adopt(store.view)
    await store.wait_ready()
    return store


def frame_dict(store: TableStore) -> dict:
    return strict_json(encode_table_data(store.frame()))["table"]


# --------------------------------------------------------------------------- #
# Seed and row shape
# --------------------------------------------------------------------------- #


class TestSeedAndShape:
    async def test_live_table_over_a_hare_cached_pv_shows_rows_older_than_subscribe(self):
        h = Harness()
        # Hare's cached value (an ``initial``) landed 5 min before the subscribe.
        await h.live("X", T0 - 300 * S, 1.0, after_connect=True, recv=T0 - 300 * S)
        await h.live("Y1", T0 - 300 * S, 2.0, after_connect=True, recv=T0 - 300 * S)
        store = await seeded_store(h, timer_spec())
        store.tick()
        table = frame_dict(store)
        assert table["tsNs"], "seeded rows expected"
        assert min(table["tsNs"]) < T0
        assert min(table["tsNs"]) >= T0 - 300 * S
        # One shared index for every column.
        assert all(len(c) == len(table["tsNs"]) for c in table["columns"].values())

    async def test_trigger_mode_one_row_per_trigger_sample_with_the_trigger_bound_last(self):
        h = Harness()
        store = await seeded_store(h, trigger_spec(y={"y1": "Y1"}, tr="{trig}"))
        h.advance(S)
        await h.live("X", h.now - 900 * MS, 1.0)
        await h.live("Y1", h.now - 900 * MS, 10.0)
        await h.live("TRIG", h.now - 800 * MS, 100.0)
        await h.live("X", h.now - 500 * MS, 2.0)
        await h.live("TRIG", h.now - 400 * MS, 101.0)
        await h.live("TRIG", h.now - 100 * MS, 102.0)
        store.tick()
        table = frame_dict(store)
        assert table["tsNs"] == [h.now - 800 * MS, h.now - 400 * MS, h.now - 100 * MS]
        assert table["columns"]["x"] == [1.0, 2.0, 2.0]
        assert table["columns"]["y1"] == [10.0, 10.0, 10.0]
        # The trigger's own value at its own sample.
        assert table["columns"]["tr"] == [100.0, 101.0, 102.0]

    async def test_cells_null_for_nan_invalid_and_non_numeric_values(self):
        h = Harness()
        spec = trigger_spec(y={"a": "A", "b": "B", "c": "C", "d": "D", "e": "E"})
        store = await seeded_store(h, spec)
        h.advance(S)
        await h.live("X", h.now - 900 * MS, float("nan"))
        await h.live("A", h.now - 900 * MS, 1.0, severity=AlarmSeverity.INVALID)
        await h.live("B", h.now - 900 * MS, [1.0, 2.0])  # waveform
        await h.live("C", h.now - 900 * MS, "3.5")  # string
        await h.live("D", h.now - 900 * MS, True)  # enum index / bool
        await h.live("E", h.now - 900 * MS, {"a": [1]})  # table-kind PV
        await h.live("TRIG", h.now - 500 * MS, 1.0)
        store.tick()
        text = encode_table_data(store.frame())
        table = strict_json(text)["table"]  # strict JSON: no NaN token anywhere
        cols = table["columns"]
        assert cols["x"] == [None]
        assert cols["a"] == [None]
        assert cols["b"] == [None]  # a waveform binding is a null column, not an error
        assert cols["c"] == [None]
        assert cols["d"] == [1.0]
        assert cols["e"] == [None]

    async def test_expression_column_inf_is_null(self):
        h = Harness()
        store = await seeded_store(h, trigger_spec(ratio="{y1} / {x}"))
        h.advance(S)
        await h.live("X", h.now - 900 * MS, 0.0)
        await h.live("Y1", h.now - 900 * MS, 1.0)
        await h.live("TRIG", h.now - 500 * MS, 1.0)
        store.tick()
        assert frame_dict(store)["columns"]["ratio"] == [None]

    async def test_interior_all_null_row_is_dropped(self):
        h = Harness()
        store = await seeded_store(h, trigger_spec())
        h.advance(S)
        await h.live("X", h.now - 900 * MS, 1.0)
        await h.live("Y1", h.now - 900 * MS, 1.0)
        await h.live("TRIG", h.now - 800 * MS, 1.0, severity=AlarmSeverity.INVALID)
        await h.live("X", h.now - 700 * MS, 1.0, severity=AlarmSeverity.INVALID)
        await h.live("Y1", h.now - 700 * MS, 1.0, severity=AlarmSeverity.INVALID)
        await h.live("TRIG", h.now - 600 * MS, 2.0, severity=AlarmSeverity.INVALID)  # all-null row
        await h.live("X", h.now - 500 * MS, 3.0)
        await h.live("TRIG", h.now - 400 * MS, 3.0)
        store.tick()
        h.advance(5 * S)
        store.tick()
        table = frame_dict(store)
        assert table["tsNs"] == [h.now - 5 * S - 800 * MS, h.now - 5 * S - 400 * MS]
        assert table["totalRows"] == 2

    async def test_an_ended_outage_nulls_only_its_interval(self):
        h = Harness()
        store = await seeded_store(h, timer_spec(), lookback_ns=60 * S)
        await h.live("X", T0 + 100 * MS, 1.0)
        await h.live("Y1", T0 + 100 * MS, 2.0)
        h.buffer.open_outage("Y1", T0 + 2 * S + 500 * MS)
        h.buffer.close_outage("Y1", T0 + 4 * S + 500 * MS)
        h.advance(7 * S)
        store.tick()
        table = frame_dict(store)
        by_ts = dict(zip(table["tsNs"], table["columns"]["y1"]))
        assert by_ts[T0 + 1 * S] == 2.0
        assert by_ts[T0 + 2 * S] == 2.0
        assert by_ts[T0 + 3 * S] is None
        assert by_ts[T0 + 4 * S] is None
        assert by_ts[T0 + 5 * S] == 2.0
        assert by_ts[T0 + 6 * S] == 2.0


# --------------------------------------------------------------------------- #
# Finalize, tail, late data
# --------------------------------------------------------------------------- #


class TestFinalizeAndTail:
    async def test_sample_after_the_rows_tick_lands_in_tail_then_final(self):
        h = Harness()
        store = await seeded_store(h, timer_spec(), lookback_ns=60 * S)
        await h.live("X", T0 + 100 * MS, 1.0)
        h.advance(S, wm=False)
        h.wm = T0
        store.tick()  # row T0+1s: tail (watermark not past it)
        assert T0 + S in store.tail_ts
        assert frame_dict(store)["columns"]["y1"][-1] is None
        # Y1's sample for that row arrives late but before the watermark passes.
        await h.live("Y1", T0 + 900 * MS, 5.0, recv=h.now + 200 * MS)
        h.advance(500 * MS, wm=False)
        store.tick()
        assert frame_dict(store)["columns"]["y1"][0] == 5.0
        assert T0 + S in store.tail_ts
        h.advance(3 * S)
        store.tick()
        assert T0 + S not in store.tail_ts
        ts, vals = store.rows.ordered()
        assert int(ts[0]) == T0 + S and vals["Y1"][0] == 5.0

    async def test_rows_beyond_the_watermark_never_change(self):
        h = Harness()
        store = await seeded_store(h, timer_spec(), lookback_ns=60 * S)
        await h.live("X", T0 + 100 * MS, 1.0)
        await h.live("Y1", T0 + 100 * MS, 1.0)
        h.advance(4 * S)
        store.tick()
        before = store.rows.ordered()
        # A sample for an already-final row (its IOC is behind): the row keeps its value.
        await h.live("Y1", T0 + 1500 * MS, 99.0, recv=h.now)
        h.advance(S)
        store.tick()
        after = store.rows.ordered()
        n = len(before[0])
        assert np.array_equal(after[0][:n], before[0])
        assert np.array_equal(after[1]["Y1"][:n], before[1]["Y1"])

    async def test_one_second_behind_ioc_finalizes_correctly(self):
        h = Harness()
        store = await seeded_store(h, timer_spec(), lookback_ns=60 * S)
        await h.live("X", T0 + 100 * MS, 1.0)
        for k in range(1, 8):
            h.advance(S)
            # Y1's IOC clock is 1 s behind the server: each sample is stamped 1 s early.
            await h.live("Y1", h.now - S, float(k))
            store.tick()
        h.advance(4 * S)
        store.tick()
        ts, vals = store.rows.ordered()
        by_ts = dict(zip(ts.tolist(), vals["Y1"].tolist()))
        # Row g holds the newest Y1 sample stamped at or before g.
        assert by_ts[T0 + 3 * S] == 4.0
        assert frame_dict(store)["inputs"]["y1"]["lateAfterFinal"] is False

    async def test_five_second_behind_ioc_is_provisional_then_final_and_late(self):
        h = Harness()
        settings = TableSettings()
        store = await seeded_store(h, timer_spec(), lookback_ns=60 * S, settings=settings)
        await h.live("X", T0 + 100 * MS, 1.0)
        h.advance(S)
        store.tick()
        assert store.tail_ts  # provisional first
        for _ in range(8):
            h.advance(S)
            await h.live("Y1", h.now - 5 * S, 2.0)
            store.tick()
        table = frame_dict(store)
        assert table["inputs"]["y1"]["lateAfterFinal"] is True
        assert table["inputs"]["x"]["lateAfterFinal"] is False
        assert store.rows.count > 0  # rows did become final
        # The state decays 60 s after the last late sample.
        last_late = store.inputs["Y1"].last_late_ns
        h.now = last_late + 59 * S
        h.wm = h.now
        store.tick()
        assert frame_dict(store)["inputs"]["y1"]["lateAfterFinal"] is True
        h.now = last_late + 61 * S
        h.wm = h.now
        store.tick()
        assert frame_dict(store)["inputs"]["y1"]["lateAfterFinal"] is False

    @pytest.mark.parametrize("case", ["reconnect", "initial", "first"])
    async def test_after_connect_samples_never_set_late(self, case):
        h = Harness()
        store = await seeded_store(h, timer_spec(), lookback_ns=60 * S)
        if case != "first":
            await h.live("Y1", T0 + 100 * MS, 1.0, after_connect=True)
        await h.live("X", T0 + 100 * MS, 1.0)
        h.advance(6 * S)
        store.tick()
        assert store.rows.count
        if case == "reconnect":
            h.buffer.open_outage("Y1", h.now - S)
            h.buffer.close_outage("Y1", h.now)
        # The sample carries an IOC ts far older than the newest final row.
        await h.live("Y1", T0 + 200 * MS, 3.0, after_connect=True)
        h.advance(S)
        store.tick()
        assert frame_dict(store)["inputs"]["y1"]["lateAfterFinal"] is False

    async def test_trigger_behind_flags_the_trigger_only_and_y_ahead_flags_that_y(self):
        h = Harness()
        store = await seeded_store(h, trigger_spec(y={"y1": "Y1", "y2": "Y2"}), lookback_ns=60 * S)
        for _ in range(6):
            h.advance(S)
            await h.live("X", h.now - 100 * MS, 1.0)
            await h.live("Y1", h.now - 100 * MS, 1.0)
            await h.live("Y2", h.now + 5 * S, 1.0)  # Y2's IOC is 5 s ahead
            await h.live("TRIG", h.now - 5 * S, 1.0)  # the trigger's IOC is 5 s behind
            store.tick()
        inputs = frame_dict(store)["inputs"]
        assert inputs["trig"]["lateAfterFinal"] is True
        assert inputs["y2"]["lateAfterFinal"] is True
        assert inputs["y1"]["lateAfterFinal"] is False
        assert inputs["x"]["lateAfterFinal"] is False

    async def test_tiny_dt_appends_at_most_n_rows_per_tick(self):
        h = Harness()
        store = await seeded_store(h, timer_spec(n=50, dt_ns=100 * MS), lookback_ns=60 * S)
        await h.live("X", T0, 1.0)
        h.advance(30 * S)  # 300 grid points due at once
        hwm = store.index_hwm
        rows_before = store.total_final + len(store.tail_ts)
        store.tick()
        appended = store.total_final + len(store.tail_ts) - rows_before
        assert 0 < appended <= 50
        assert store.index_hwm == hwm + 300 * 100 * MS  # the backlog is skipped, not queued
        assert len(frame_dict(store)["tsNs"]) <= 50

    async def test_flush_marker_keeps_a_quiet_timer_tail_short(self):
        h = Harness()
        store = await seeded_store(h, timer_spec(), lookback_ns=60 * S)
        await h.live("X", T0, 1.0)
        await h.live("Y1", T0, 1.0)
        for _ in range(30):
            h.advance(S)  # flush markers keep the watermark moving with no data
            store.tick()
        assert len(store.tail_ts) <= 3

    async def test_frame_handed_to_the_encoder_is_unchanged_by_the_next_tick(self):
        h = Harness()
        store = await seeded_store(h, timer_spec(n=5), lookback_ns=60 * S)
        await h.live("X", T0, 1.0)
        await h.live("Y1", T0, 1.0)
        for _ in range(6):
            h.advance(S)
            store.tick()
        frame = store.frame()
        snapshot = encode_table_data(frame)
        for k in range(10):
            h.advance(S)
            await h.live("X", h.now, float(k + 10))
            store.tick()
        assert encode_table_data(frame) == snapshot

    async def test_store_arrays_total_at_most_1mb_at_10000_by_9(self):
        h = Harness()
        ys = {f"y{i}": f"Y{i}" for i in range(1, 9)}
        store = TableStore(make_view(timer_spec(n=10_000, y=ys)), TableSettings(), h.port())
        for pv in store.view.pvs:
            store.rows.ensure_key(pv)
        assert store.nbytes() <= 1_000_000

    async def test_frames_never_exceed_n_rows_with_a_non_empty_tail(self):
        h = Harness()
        store = await seeded_store(h, timer_spec(n=10_000, dt_ns=100 * MS), lookback_ns=HOUR)
        await h.live("X", T0, 1.0)
        for _ in range(3):
            h.advance(700 * S, wm=False)
            h.wm = h.now - 1 * S
            store.tick()
            assert store.tail_ts
            assert len(frame_dict(store)["tsNs"]) <= 10_000
        h.advance(S)
        store.tick()
        table = frame_dict(store)
        assert len(table["tsNs"]) == 10_000
        assert table["totalRows"] > len(table["tsNs"])  # "N of M rows shown"


# --------------------------------------------------------------------------- #
# Seed outcomes
# --------------------------------------------------------------------------- #


class TestSeedOutcomes:
    async def test_failed_y_seed_is_live_only_and_the_retry_patches_held_rows(self):
        h = Harness()
        h.archived("X", [(T0 - 50 * S, 1.0)])
        h.archived("Y1", [(T0 - 50 * S, 7.0)])
        h.errors["Y1"] = ArchiverUnavailable("down")
        store = await seeded_store(h, timer_spec(), lookback_ns=30 * S)
        store.tick()
        table = frame_dict(store)
        assert table["inputs"]["y1"]["history"] == "live-only"
        assert set(table["columns"]["y1"]) == {None}
        assert store.due_retries(h.now) == []
        del h.errors["Y1"]
        h.advance(61 * S)
        due = store.due_retries(h.now)
        assert due == ["Y1"]
        version = store.version
        store.start_retry(due)
        await store.wait_ready()
        assert store.version > version
        store.tick()
        table = frame_dict(store)
        assert table["inputs"]["y1"]["history"] == "seeded"
        assert 7.0 in table["columns"]["y1"]

    @pytest.mark.parametrize("cause", ["mole-off", "unknown-pv"])
    async def test_not_archived_is_terminal(self, cause):
        h = Harness()
        if cause == "mole-off":
            h.archiver = False
        else:
            h.errors["Y1"] = PVNotFoundInArchiver("no such pv")
        h.archived("X", [(T0 - 50 * S, 1.0)])
        store = await seeded_store(h, timer_spec(), lookback_ns=30 * S)
        store.tick()
        assert frame_dict(store)["inputs"]["y1"]["history"] == "not-archived"
        h.advance(120 * S)
        assert store.due_retries(h.now) == []
        assert len(h.mole_calls("Y1")) == (0 if cause == "mole-off" else 1)

    async def test_seed_takes_the_newer_of_ring_and_archive(self):
        h = Harness()
        # The archive lags 10 s; the ring holds a step 5 s before the subscribe.
        h.archived("X", [(T0 - 120 * S, 1.0), (T0 - 10 * S, 1.0)])
        h.archived("Y1", [(T0 - 120 * S, 1.0), (T0 - 10 * S, 1.0)])
        await h.live("Y1", T0 - 5 * S, 2.0, after_connect=True, recv=T0 - 5 * S)
        store = await seeded_store(h, timer_spec(), lookback_ns=60 * S)
        store.tick()
        table = frame_dict(store)
        by_ts = dict(zip(table["tsNs"], table["columns"]["y1"]))
        assert by_ts[T0 - 6 * S] == 1.0
        assert by_ts[T0 - 5 * S] == 2.0
        assert by_ts[T0 - 2 * S] == 2.0

    async def test_polled_archive_trigger_rows_only_at_value_changes(self):
        h = Harness()
        h.faithful = False
        # A 10 s recorder: the trigger value changes at -60 s and -30 s only.
        snaps = [(T0 - k * 10 * S, 1.0 if k > 6 else (2.0 if k > 3 else 3.0)) for k in range(10, 0, -1)]
        h.archived("TRIG", snaps)
        h.archived("X", [(T0 - 200 * S, 5.0, AlarmSeverity.INVALID)])
        h.archived("Y1", [(T0 - 200 * S, 6.0)])
        store = await seeded_store(h, trigger_spec(), lookback_ns=HOUR)
        table = frame_dict(store)
        assert table["tsNs"] == [T0 - 60 * S, T0 - 30 * S]
        # A polled archive carries no severity: no INVALID nulls in seeded cells.
        assert table["columns"]["x"] == [5.0, 5.0]

    async def test_single_trigger_sample_changes_the_content_once(self):
        h = Harness()
        store = await seeded_store(h, trigger_spec(), lookback_ns=60 * S)
        await h.live("X", T0, 1.0)
        await h.live("Y1", T0, 1.0)
        store.tick()
        h.advance(100 * MS)
        await h.live("TRIG", h.now, 1.0)
        assert store.tick() is True
        changes = 0
        for _ in range(10):
            h.advance(500 * MS)
            changes += store.tick()
        assert changes == 0
        assert store.rows.count == 1  # it did finalize, silently

    async def test_silent_trigger_with_empty_archive_gives_zero_rows(self):
        h = Harness()
        store = await seeded_store(h, trigger_spec(), lookback_ns=60 * S)
        store.tick()
        table = frame_dict(store)
        assert table["totalRows"] == 0 and table["tsNs"] == []
        assert set(table["inputs"]) == {"x", "y1", "trig"}
        assert table["inputs"]["trig"]["status"] == "no-data"
        changes = 0
        for _ in range(5):
            h.advance(S)
            changes += store.tick()
        assert changes == 0

    async def test_sparse_trigger_dense_archived_y_over_24h_is_seeded(self):
        h = Harness()
        day = 24 * HOUR
        h.archived("TRIG", [(T0 - day + k * 86 * S, float(k)) for k in range(1000)])
        h.archived("X", [(T0 - day - S, 1.0)])

        dense_calls = []

        async def dense_y(pv, t0, t1, *, budget=None):
            # A 100 Hz PV: return a thin, representative block (values = ts seconds).
            dense_calls.append((t0, t1))
            h.calls.append(("window", pv, t0, t1))
            ts = np.arange(max(t0, T0 - day - S), t1 + 1, 50 * S, dtype=np.int64)
            return ArchiveBlock(ts, ts.astype(object), np.zeros(ts.size, np.int8))

        port = h.port()
        orig = port.fetch_window_budgeted

        async def routed(pv, t0, t1, *, budget=None):
            return await (dense_y if pv == "Y1" else orig)(pv, t0, t1, budget=budget)

        port.fetch_window_budgeted = routed
        store = TableStore(make_view(trigger_spec(n=1000), day), TableSettings(), port)
        store.adopt(store.view)
        await store.wait_ready()
        table = frame_dict(store)
        assert len(table["tsNs"]) == 1000
        assert table["inputs"]["y1"]["history"] == "seeded"
        assert None not in table["columns"]["y1"]

    async def test_rows_between_the_archives_newest_trigger_and_subscribe_exist(self):
        h = Harness()
        # The archive is 10 s behind; the live ring has the trigger's newer samples.
        h.archived("TRIG", [(T0 - 60 * S + k * S, float(k)) for k in range(50)])  # up to T0 - 11 s
        h.archived("X", [(T0 - HOUR, 1.0)])
        h.archived("Y1", [(T0 - HOUR, 1.0)])
        for k in range(10, 0, -1):
            await h.live("TRIG", T0 - k * S, float(100 - k), recv=T0 - k * S + 50 * MS)
        store = await seeded_store(h, trigger_spec(), lookback_ns=HOUR)
        store.tick()
        ts = frame_dict(store)["tsNs"]
        for k in range(10, 0, -1):
            assert T0 - k * S in ts
        assert T0 - 11 * S in ts

    async def test_total_rows_exceeds_the_frame_once_more_than_n_rows_were_produced(self):
        h = Harness()
        store = await seeded_store(h, trigger_spec(n=3), lookback_ns=60 * S)
        await h.live("X", T0, 1.0)
        for _ in range(5):
            h.advance(S)
            await h.live("TRIG", h.now, 1.0)
            store.tick()
        h.advance(5 * S)
        store.tick()
        table = frame_dict(store)
        assert len(table["tsNs"]) == 3
        assert table["totalRows"] == 5


# --------------------------------------------------------------------------- #
# Emit rule
# --------------------------------------------------------------------------- #


class TestEmitRule:
    async def test_timer_changes_each_interval_with_a_non_null_row_and_not_for_all_null(self):
        h = Harness()
        store = await seeded_store(h, timer_spec(), lookback_ns=60 * S)
        store.tick()
        # No input has data yet: every new grid row is all-null and dropped.
        for _ in range(3):
            h.advance(S)
            assert store.tick() is False
        await h.live("X", h.now, 1.0)
        for _ in range(3):
            h.advance(S)
            assert store.tick() is True

    async def test_late_flag_flip_counts_as_a_change(self):
        h = Harness()
        store = await seeded_store(h, timer_spec(), lookback_ns=60 * S)
        await h.live("X", T0, 1.0)
        h.advance(S)
        store.tick()
        h.advance(S)
        await h.live("Y1", h.now + 10 * S, 1.0)  # skewed IOC
        assert store.tick() is True
        assert store.frame().inputs["y1"][2] is True

    async def test_frame_validates_against_the_rest_dto(self):
        from canopy.formula_engine.schemas.formula import FormulaResultDTO

        h = Harness()
        store = await seeded_store(h, trigger_spec(), lookback_ns=60 * S)
        await h.live("X", T0, math.pi)
        h.advance(S)
        await h.live("TRIG", h.now, 1.0)
        store.tick()
        data = strict_json(encode_table_data(store.frame()))
        dto = FormulaResultDTO.model_validate(data)
        assert dto.table is not None
        assert set(data) == {"kind", "table"}


class TestTriggerHistory:
    async def test_an_empty_trigger_archive_is_live_only_and_never_retried(self):
        h = Harness()
        store = await seeded_store(h, trigger_spec(), lookback_ns=60 * S)
        assert store.inputs["TRIG"].history == "live-only"
        h.advance(300 * S)
        assert store.due_retries(h.now) == []

    async def test_a_failed_trigger_fetch_is_retried_and_prepends_the_rows(self):
        h = Harness()
        h.archived("TRIG", [(T0 - 30 * S + k * S, float(k)) for k in range(20)])
        h.archived("X", [(T0 - HOUR, 1.0)])
        h.errors["TRIG"] = ArchiverUnavailable("down")
        store = await seeded_store(h, trigger_spec(), lookback_ns=HOUR)
        assert store.inputs["TRIG"].history == "live-only"
        assert store.rows.count == 0
        del h.errors["TRIG"]
        h.advance(61 * S)
        due = store.due_retries(h.now)
        assert due == ["TRIG"]
        store.start_retry(due)
        await store.wait_ready()
        store.tick()
        table = frame_dict(store)
        assert table["tsNs"][0] == T0 - 30 * S
        assert len(table["tsNs"]) == 20
        assert store.inputs["TRIG"].history in ("seeded", "truncated")

    async def test_fewer_trigger_samples_than_n_within_the_lookback_is_truncated(self):
        h = Harness()
        h.archived("TRIG", [(T0 - 30 * S + k * S, float(k)) for k in range(20)])
        h.archived("X", [(T0 - HOUR, 1.0)])
        store = await seeded_store(h, trigger_spec(n=100), lookback_ns=60 * S)
        assert frame_dict(store)["inputs"]["trig"]["history"] == "truncated"


def edge_spec(edge: str | None, trig: str = "TRIG", *, trigger_only: bool = False) -> FormulaSpec:
    if trigger_only:
        # The trigger is the only input: once it moves past a row, every input has.
        return FormulaSpec(
            kind="table",
            bindings={"trig": trig},
            columns={"t": "{trig}"},
            index="first_input",
            trigger="trig",
            max_points=1000,
            trigger_edge=edge,
        )
    return FormulaSpec(**{**trigger_spec(trig=trig).model_dump(), "trigger_edge": edge})


class TestIndexKey:
    """stores are reused and copied only between subscriptions with the same edge rule."""

    def test_the_view_carries_the_spec_edge(self):
        assert make_view(edge_spec("rising")).trigger_edge == "rising"
        assert make_view(edge_spec("falling")).trigger_edge == "falling"
        assert make_view(trigger_spec()).trigger_edge is None
        assert make_view(timer_spec()).trigger_edge is None

    def test_an_every_update_key_equals_the_edgeless_key(self):
        assert make_view(trigger_spec()).index_key == "trigger:TRIG"
        assert make_view(timer_spec(dt_ns=S)).index_key == f"grid:{S}"

    def test_every_update_and_rising_on_one_trigger_get_different_stores(self):
        every, rising = make_view(edge_spec(None)), make_view(edge_spec("rising"))
        assert every.trigger_pv == rising.trigger_pv == "TRIG"
        assert every.index_key != rising.index_key

    def test_rising_and_falling_on_one_trigger_get_different_stores(self):
        assert make_view(edge_spec("rising")).index_key != make_view(edge_spec("falling")).index_key

    def test_two_rising_subscriptions_share_one_store(self):
        assert make_view(edge_spec("rising")).index_key == make_view(edge_spec("rising")).index_key

    def test_the_edge_key_never_collides_with_a_trigger_pv_name(self):
        rising = make_view(edge_spec("rising")).index_key
        for pv in ("TRIG:rising", "TRIG:edge=rising", "TRIG|rising", "rising:TRIG", "-rising:TRIG"):
            assert make_view(edge_spec(None, trig=pv)).index_key != rising


# --------------------------------------------------------------------------- #
# Edge detection
# --------------------------------------------------------------------------- #

#: Trigger samples relative to a base time: an intra-bucket 0->1->0->1->0 at 3.01-3.04 s.
EDGE_SEQ = [
    (0, 0.0),
    (1000 * MS, 1.0),
    (2000 * MS, 0.0),
    (3010 * MS, 1.0),
    (3020 * MS, 0.0),
    (3030 * MS, 1.0),
    (3040 * MS, 0.0),
    (4000 * MS, 1.0),
]
#: Rows per edge: two edges in one 100 ms bucket coalesce to the last one.
EDGE_ROWS = {
    "rising": [1000 * MS, 3030 * MS, 4000 * MS],
    "falling": [2000 * MS, 3040 * MS],
}


async def edge_harness() -> Harness:
    h = Harness()
    # Every row needs a non-null cell: X has a ring sample before everything.
    await h.live("X", T0 - 120 * S, 1.0, after_connect=True, recv=T0 - 120 * S)
    return h


async def edge_live_store(h: Harness, edge: str, *, trigger_only: bool = False) -> TableStore:
    """A seeded store with nothing to seed, whose first tick (the backfill) already ran."""
    store = await seeded_store(h, edge_spec(edge, trigger_only=trigger_only), lookback_ns=60 * S)
    store.tick()
    return store


async def feed(h: Harness, store: TableStore, base: int, seq, *, tick_each: bool = True) -> None:
    for off, value in seq:
        h.now = max(h.now, base + off + MS)
        h.wm = h.now
        await h.live("TRIG", base + off, value, recv=h.now)
        if tick_each:
            store.tick()


def settle(h: Harness, store: TableStore) -> dict:
    h.advance(10 * S)
    store.tick()
    return frame_dict(store)


class TestTriggerEdges:
    """edges between consecutive trigger samples in time order, whatever their source."""

    @pytest.mark.parametrize("trigger_only", [False, True], ids=["with-static-inputs", "trigger-only"])
    @pytest.mark.parametrize("edge", ["rising", "falling"])
    async def test_intra_bucket_edges_give_identical_rows_on_seed_live_and_backfill(self, edge, trigger_only):
        # trigger-only: every input moves past a row as soon as the trigger does, so the
        # live path must hold an edge row while its flush bucket is still open.
        expected = EDGE_ROWS[edge]
        spec = edge_spec(edge, trigger_only=trigger_only)
        results = {}

        # Archive seed.
        h = await edge_harness()
        base = T0 - 20 * S
        h.archived("TRIG", [(base + off, v) for off, v in EDGE_SEQ])
        store = await seeded_store(h, spec, lookback_ns=60 * S)
        results["archive seed"] = [t - base for t in settle(h, store)["tsNs"]]

        # Ring seed: the samples are in Vole's ring before the subscribe.
        h = await edge_harness()
        for off, v in EDGE_SEQ:
            await h.live("TRIG", base + off, v, recv=base + off)
        store = await seeded_store(h, spec, lookback_ns=60 * S)
        results["ring seed"] = [t - base for t in settle(h, store)["tsNs"]]

        # Live: one tick per sample, so the intra-bucket edges span ticks.
        h = await edge_harness()
        store = await edge_live_store(h, edge, trigger_only=trigger_only)
        base = T0 + S
        await feed(h, store, base, EDGE_SEQ)
        results["live"] = [t - base for t in settle(h, store)["tsNs"]]

        # Backfill: every sample lands before the store's first tick.
        h = await edge_harness()
        store = await seeded_store(h, spec, lookback_ns=60 * S)
        await feed(h, store, base, EDGE_SEQ, tick_each=False)
        results["backfill"] = [t - base for t in settle(h, store)["tsNs"]]

        assert results == {k: expected for k in results}

    async def test_none_between_two_numbers_neither_fires_nor_breaks_the_pair(self):
        h = await edge_harness()
        store = await edge_live_store(h, "rising")
        base = T0 + S
        seq = [(0, 0.0), (500 * MS, None), (1000 * MS, 1.0), (1500 * MS, None), (2000 * MS, 1.0)]
        await feed(h, store, base, seq)
        # 0 -> 1 fires across the None; 1 -> 1 across the second None does not.
        assert settle(h, store)["tsNs"] == [base + 1000 * MS]

    async def test_enum_label_trigger_never_fires_and_never_updates_the_predecessor(self):
        h = await edge_harness()
        store = await edge_live_store(h, "rising")
        base = T0 + S
        seq = [
            (0, "OFF"),
            (500 * MS, "ON"),
            (1000 * MS, "OFF"),
            (1500 * MS, 0),
            (2000 * MS, "ON"),  # a label between 0 and 1: 0 stays the predecessor
            (2500 * MS, b"\x01"),
            (3000 * MS, 1),
        ]
        await feed(h, store, base, seq)
        assert settle(h, store)["tsNs"] == [base + 3000 * MS]

    async def test_bool_trigger_coerces_to_int(self):
        h = await edge_harness()
        store = await edge_live_store(h, "falling")
        base = T0 + S
        await feed(h, store, base, [(0, True), (500 * MS, False), (1000 * MS, 1), (1500 * MS, False)])
        assert settle(h, store)["tsNs"] == [base + 500 * MS, base + 1500 * MS]

    @pytest.mark.parametrize("edge", ["rising", "falling"])
    async def test_the_oldest_sample_ever_seen_never_fires(self, edge):
        # Seed: one archived sample, nothing before it.
        h = await edge_harness()
        h.archived("TRIG", [(T0 - 10 * S, 5.0)])
        store = await seeded_store(h, edge_spec(edge), lookback_ns=60 * S)
        assert settle(h, store)["tsNs"] == []
        # Live: the store's very first sample.
        h = await edge_harness()
        store = await edge_live_store(h, edge)
        await feed(h, store, T0 + S, [(0, 5.0 if edge == "rising" else -5.0)])
        assert settle(h, store)["tsNs"] == []

    @pytest.mark.parametrize("source", ["archive", "ring"])
    async def test_seed_to_live_boundary_carries_the_predecessor(self, source):
        h = await edge_harness()
        seeded = [(T0 - 5 * S, 0.0), (T0 - 3 * S, 1.0)]
        if source == "archive":
            h.archived("TRIG", seeded)
        else:
            for t, v in seeded:
                await h.live("TRIG", t, v, recv=t)
        store = await seeded_store(h, edge_spec("rising"), lookback_ns=60 * S)
        store.tick()
        # The first live sample fires against the seed's newest one (1 -> 2), and
        # the seed's samples are not decided again.
        await feed(h, store, T0 + S, [(0, 2.0), (S, 2.0), (2 * S, 1.0), (3 * S, 3.0)])
        assert settle(h, store)["tsNs"] == [T0 - 3 * S, T0 + S, T0 + 4 * S]

    async def test_a_ring_seed_takes_the_newest_numeric_sample_before_the_lookback_as_predecessor(self):
        h = await edge_harness()
        # Before the lookback: 0, then a label; inside it: 1 (fires against the 0).
        for t, v in [(T0 - 90 * S, 0.0), (T0 - 70 * S, "OFF"), (T0 - 10 * S, 1.0)]:
            await h.live("TRIG", t, v, recv=t)
        store = await seeded_store(h, edge_spec("rising"), lookback_ns=60 * S)
        assert settle(h, store)["tsNs"] == [T0 - 10 * S]

    async def test_a_copied_store_inherits_the_predecessor(self):
        h = await edge_harness()
        source = await edge_live_store(h, "rising")
        await feed(h, source, T0 + S, [(0, 0.0), (S, 1.0)])
        h.advance(5 * S)
        source.tick()
        view = make_view(edge_spec("rising"), 60 * S)
        copy = TableStore(view, TableSettings(), h.port())
        copy.adopt(view, source=source)
        await copy.wait_ready()
        copy.tick()
        # The copy's first sample fires against the source's predecessor (1 -> 2).
        base = h.now
        await feed(h, copy, base, [(S, 2.0)])
        assert settle(h, copy)["tsNs"] == [T0 + 2 * S, base + S]

    async def test_every_update_keeps_a_row_per_sample(self):
        h = await edge_harness()
        store = await edge_live_store(h, None)
        base = T0 + S
        await feed(h, store, base, [(0, 0.0), (500 * MS, None), (1000 * MS, "ON"), (1500 * MS, 1.0)])
        assert settle(h, store)["tsNs"] == [base, base + 500 * MS, base + 1000 * MS, base + 1500 * MS]


# --------------------------------------------------------------------------- #
# Edge seed reach: fetch until n edges, not n samples
# --------------------------------------------------------------------------- #

#: A 1 Hz trigger over the last 1000 s that is 0 except one 1 every 100 s:
#: one rising edge per 100 s (at k = 50, 150, ..., 950), far sparser than its samples.
SPARSE_K = 1000


def sparse_ts(k: int, base: int = T0 - SPARSE_K * S) -> int:
    return base + k * S


def sparse_seq() -> list[tuple[int, float]]:
    return [(k * S, 1.0 if k % 100 == 50 else 0.0) for k in range(SPARSE_K)]


def sparse_spec(edge: str | None, n: int) -> FormulaSpec:
    return FormulaSpec(**{**edge_spec(edge).model_dump(), "max_points": n})


async def sparse_harness() -> Harness:
    h = await edge_harness()
    h.archived("X", [(T0 - 2 * HOUR, 1.0)])
    h.archived("TRIG", [(sparse_ts(0) + off, v) for off, v in sparse_seq()])
    return h


class TestEdgeSeedReach:
    async def test_a_sparse_rising_trigger_seeds_n_edges_across_several_blocks(self):
        h = await sparse_harness()
        store = await seeded_store(h, sparse_spec("rising", 5), lookback_ns=HOUR)
        table = settle(h, store)
        assert table["tsNs"] == [sparse_ts(k) for k in (550, 650, 750, 850, 950)]
        assert table["inputs"]["trig"]["history"] == "seeded"
        # The first 60 s window already held far more than 5 samples, but only one edge.
        assert len(h.mole_calls("TRIG")) > 1

    async def test_the_lookback_bound_stops_the_seed_with_fewer_rows(self):
        h = await sparse_harness()
        store = await seeded_store(h, sparse_spec("rising", 5), lookback_ns=250 * S)
        table = settle(h, store)
        assert table["tsNs"] == [sparse_ts(k) for k in (750, 850, 950)]
        assert table["inputs"]["trig"]["history"] == "truncated"
        windows = [c for c in h.mole_calls("TRIG") if c[0] == "window"]
        assert min(c[2] for c in windows) == T0 - 250 * S

    async def test_every_update_seeds_exactly_as_before(self):
        h = await sparse_harness()
        store = await seeded_store(h, sparse_spec(None, 5), lookback_ns=HOUR)
        table = settle(h, store)
        # The newest 5 samples, from one window: the edge reach does not apply.
        assert table["tsNs"] == [sparse_ts(k) for k in range(995, 1000)]
        assert [c[0] for c in h.mole_calls("TRIG")] == ["window"]

    async def test_a_backfill_re_read_gives_the_same_rows_as_the_live_path(self):
        expected = [k * S for k in (550, 650, 750, 850, 950)]
        # Archive seed across several blocks.
        h = await sparse_harness()
        store = await seeded_store(h, sparse_spec("rising", 5), lookback_ns=HOUR)
        seeded = [t - sparse_ts(0) for t in settle(h, store)["tsNs"]]
        # Backfill: the same samples land live before the store's first tick.
        h = await edge_harness()
        store = await seeded_store(h, sparse_spec("rising", 5), lookback_ns=60 * S)
        base = T0 + S
        await feed(h, store, base, sparse_seq(), tick_each=False)
        backfilled = [t - base for t in settle(h, store)["tsNs"]]
        assert seeded == backfilled == expected
