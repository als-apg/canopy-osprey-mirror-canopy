"""Pins, settle-depth capacity, provisioning on the ring and the pinned memory bound.

What Vole guarantees live evals without Hare ingest: a pinned ring survives
grace and LRU eviction, unpin never arms grace, ``ensure_capacity`` only grows,
provisioning lives on the ring (dies with it, is retried after
``provision_retry_s`` on refusal, is cleared by a pressure shrink), and when only
pinned rings remain over ``global_bytes_max`` they shrink to ``settle_depth``
instead of being dropped.
"""

from __future__ import annotations

import asyncio

import pytest

from canopy.core.clock import FakeClock
from canopy.vole.config import VoleConfig
from canopy.vole.errors import PVNotBuffered
from canopy.vole.adapter import VoleDataSource
from canopy.contracts.sample import Sample, AlarmSeverity
from canopy.vole.buffer.ring import BYTES_PER_SAMPLE
from canopy.vole.buffer.store import PVStore
from tests.canopy.vole.conftest import FakeIngestService
from canopy.vole.services.grace_manager import GraceManager
from canopy.vole.services.buffer_service import BufferService
from canopy.vole.services.eviction_service import EvictionService
from canopy.vole.services.alignment_service import AlignmentService

S = 1_000_000_000
NOW = 1_000_000_000_000


def _sample(ts_ns: int, value=1.0) -> Sample:
    return Sample(ts_ns=ts_ns, value=value, severity=AlarmSeverity.NO_ALARM, source="live")


def _service(config: VoleConfig, clock: FakeClock | None = None) -> BufferService:
    store = PVStore(config)
    eviction = EvictionService(store, config)
    return BufferService(store, AlignmentService(), eviction, clock or FakeClock(start_ns=NOW), config)


async def _settle() -> None:
    for _ in range(5):
        await asyncio.sleep(0)


# --- pins ---------------------------------------------------------------------- #


class TestPins:
    async def test_pin_and_unpin_registry(self, test_service):
        test_service.pin(["A", "ca://B"], holder="t1")
        test_service.pin(["B"], holder="t2")
        assert test_service.is_pinned("A") and test_service.is_pinned("B")
        assert test_service.unpin("t1") == ["A"]  # B is still held by t2
        assert not test_service.is_pinned("A") and test_service.is_pinned("B")
        assert test_service.unpin("t2") == ["B"]
        assert test_service.unpin("t2") == []  # idempotent

    async def test_pins_are_keyed_by_the_stripped_name(self, test_service):
        test_service.pin(["ca://SR:X"], holder="t")
        assert test_service.is_pinned("SR:X")
        assert test_service.is_pinned("ca://SR:X")

    async def test_pinned_ring_survives_grace(self, vole_config, test_service):
        config = VoleConfig(max_points=1000, global_bytes_max=1_000_000, buffer_grace_seconds=0)
        ingest = FakeIngestService()
        grace = GraceManager(ingest, test_service, config, is_pinned=test_service.is_pinned)
        await ingest.subscribe(["P"])
        await test_service.append("P", _sample(S))
        test_service.pin(["P"], holder="table")

        await grace.on_pv_deactivated(["P"])  # last browser client left
        await _settle()
        assert test_service.read_latest_sync("P") is not None
        assert "P" in ingest.subscribed_pvs

    async def test_ca_prefixed_binding_pin_survives_grace(self, test_service):
        config = VoleConfig(max_points=1000, global_bytes_max=1_000_000, buffer_grace_seconds=0)
        ingest = FakeIngestService()
        grace = GraceManager(ingest, test_service, config, is_pinned=test_service.is_pinned)
        await ingest.subscribe(["SR:X"])
        await test_service.append("SR:X", _sample(S))
        test_service.pin(["ca://SR:X"], holder="table")

        await grace.on_pv_deactivated(["SR:X"])
        await _settle()
        assert test_service.read_latest_sync("SR:X") is not None

    async def test_prefixed_deactivation_is_rearmed_by_the_unprefixed_unpin(self, test_service):
        """Hare may deactivate ``ca://P`` while the pin (and unpin's release) name ``P``."""
        config = VoleConfig(max_points=1000, global_bytes_max=1_000_000, buffer_grace_seconds=0)
        ingest = FakeIngestService()
        grace = GraceManager(ingest, test_service, config, is_pinned=test_service.is_pinned)
        await ingest.subscribe(["ca://P"])
        test_service.pin(["P"], holder="t")

        await grace.on_pv_deactivated(["ca://P"])
        await _settle()
        assert "ca://P" in ingest.subscribed_pvs  # skipped while pinned
        await grace.on_pvs_unpinned(test_service.unpin("t"))
        await _settle()
        assert "ca://P" not in ingest.subscribed_pvs  # the skipped expiry was re-armed

    async def test_unpin_never_arms_grace_but_rearms_a_skipped_expiry(self, test_service):
        """A REST-only ring's lifetime is unchanged by pin/unpin."""
        config = VoleConfig(max_points=1000, global_bytes_max=1_000_000, buffer_grace_seconds=0)
        ingest = FakeIngestService()
        grace = GraceManager(ingest, test_service, config, is_pinned=test_service.is_pinned)
        await ingest.subscribe(["P", "Q"])
        await test_service.append("P", _sample(S))
        await test_service.append("Q", _sample(S))

        # Q: pinned and unpinned while still held by a browser client -> stays.
        test_service.pin(["Q"], holder="t")
        await grace.on_pvs_unpinned(test_service.unpin("t"))
        await _settle()
        assert test_service.read_latest_sync("Q") is not None

        # P: deactivation expiry skipped while pinned; the last unpin re-arms it.
        test_service.pin(["P"], holder="t1")
        test_service.pin(["P"], holder="t2")
        await grace.on_pv_deactivated(["P"])
        await _settle()
        assert test_service.read_latest_sync("P") is not None
        await grace.on_pvs_unpinned(test_service.unpin("t1"))
        await _settle()
        assert test_service.read_latest_sync("P") is not None  # still pinned by t2
        await grace.on_pvs_unpinned(test_service.unpin("t2"))
        await _settle()
        assert test_service.read_latest_sync("P") is None
        assert "P" not in ingest.subscribed_pvs

    async def test_reactivation_forgets_a_skipped_expiry(self, test_service):
        config = VoleConfig(max_points=1000, global_bytes_max=1_000_000, buffer_grace_seconds=0)
        ingest = FakeIngestService()
        grace = GraceManager(ingest, test_service, config, is_pinned=test_service.is_pinned)
        await ingest.subscribe(["P"])
        await test_service.append("P", _sample(S))
        test_service.pin(["P"], holder="t")
        await grace.on_pv_deactivated(["P"])
        await _settle()
        await grace.on_pv_activated(["P"])  # a browser client is back
        await grace.on_pvs_unpinned(test_service.unpin("t"))
        await _settle()
        assert test_service.read_latest_sync("P") is not None

    async def test_lru_eviction_never_drops_a_pinned_ring(self):
        config = VoleConfig(max_points=100, global_bytes_max=BYTES_PER_SAMPLE * 4)
        store = PVStore(config)
        dropped_pvs: list[str] = []

        async def on_evict(pvs):
            dropped_pvs.extend(pvs)

        eviction = EvictionService(store, config, on_evict=on_evict)
        service = BufferService(store, AlignmentService(), eviction, FakeClock(start_ns=NOW), config)
        service.pin(["PINNED"], holder="t")
        for i in range(3):
            await service.append("PINNED", _sample((i + 1) * S))
        service.touch("PINNED", 1)  # oldest access: the first LRU victim without the pin
        await service.append("OTHER", _sample(S))
        service.touch("OTHER", 2)
        await service.append("OTHER", _sample(2 * S))  # over the ceiling: eviction runs

        assert "PINNED" not in dropped_pvs
        assert store.contains("PINNED")


# --- capacity ------------------------------------------------------------------ #


class TestCapacity:
    async def test_ensure_capacity_grows_a_size_one_ring_to_settle_depth(self, test_service):
        await test_service.seed("P", [], max_points=1)  # what subscribe_latest creates
        test_service.ensure_capacity("P")
        assert test_service.ring_max_points("P") == 1024

    async def test_ensure_capacity_never_shrinks(self):
        config = VoleConfig(max_points=500_000, global_bytes_max=4_294_967_296)
        service = _service(config)
        await service.seed("P", [], max_points=500_000)
        service.ensure_capacity("P", 1024)
        assert service.ring_max_points("P") == 500_000

    async def test_missing_ring_is_created_at_config_max_points(self, test_service, vole_config):
        test_service.ensure_capacity("NEW", 10)  # below config.max_points: creation size wins
        assert test_service.ring_max_points("NEW") == vole_config.max_points

    async def test_scalar_subscribe_latest_leaves_a_table_ring_unchanged(self, test_service):
        test_service.ensure_capacity("P", 1024)
        await test_service.seed("P", [], max_points=1)  # subscribe_latest's seed on an existing ring
        assert test_service.ring_max_points("P") == 1024

    async def test_gateway_ensure_capacity_strips_the_protocol_prefix(self, test_service):
        from canopy.vole import gateway

        await test_service.seed("SR:X", [], max_points=1)
        gateway.bind(test_service)
        try:
            gateway.ensure_capacity("ca://SR:X")
        finally:
            gateway.bind(None)
        assert test_service.ring_max_points("SR:X") == 1024
        assert test_service.ring_max_points("ca://SR:X") is None  # no phantom ring

    async def test_read_window_is_the_same_with_and_without_a_table(self, test_service):
        for i in range(1, 6):
            await test_service.append("P", _sample(i * S, float(i)))
        before = await test_service.read_window("P", 0, 10 * S)
        test_service.pin(["P"], holder="table")
        test_service.ensure_capacity("P", 1024)
        during = await test_service.read_window("P", 0, 10 * S)
        test_service.unpin("table")
        after = await test_service.read_window("P", 0, 10 * S)
        assert before == during == after


# --- provisioning on the ring -------------------------------------------------- #


class _Mole:
    """Fake ``fetch_window``: a history per PV, a refused set, and a call log."""

    def __init__(self, history: dict[str, list[Sample]] | None = None, refuse: set[str] | None = None) -> None:
        self.history = history or {}
        self.refuse = refuse or set()
        self.calls: list[str] = []

    async def fetch_window(self, pv, t0_ns, t1_ns, *, max_points=None):
        self.calls.append(pv)
        if pv in self.refuse:
            raise RuntimeError(f"{pv} not archived")
        return [s for s in self.history.get(pv, []) if t0_ns <= s.ts_ns <= t1_ns]


def _archive(count: int, *, end_ns: int) -> list[Sample]:
    return [
        Sample(ts_ns=end_ns - (count - i) * S, value=float(i), severity=AlarmSeverity.NO_ALARM, source="archive")
        for i in range(count)
    ]


@pytest.fixture
def clock():
    return FakeClock(start_ns=NOW)


@pytest.fixture
def mole():
    return _Mole()


def _source(service: BufferService, mole: _Mole, clock: FakeClock, subscribed: list[str]) -> VoleDataSource:
    async def subscribe_live(pvs):
        subscribed.extend(pvs)

    return VoleDataSource(service, fetch_window=mole.fetch_window, subscribe_live=subscribe_live, clock=clock)


class TestProvisioning:
    async def test_table_ring_then_timeseries_has_archive_history(self, test_service, mole, clock):
        """A ring created by a table's live ingest is provisioned by the next timeseries reader."""
        mole.history["P"] = _archive(10, end_ns=NOW - 100 * S)
        subscribed: list[str] = []
        source = _source(test_service, mole, clock, subscribed)
        # The table: capacity, pin and live ingest only -- it never provisions.
        test_service.pin(["P"], holder="table")
        test_service.ensure_capacity("P", 1024)
        await test_service.append("P", _sample(NOW - S, 42.0), recv_ns=1)

        window = await source.read_aligned(["P"], NOW - 3600 * S, NOW, align="last", index="union")
        assert window.ts_ns.size == 11  # ten archived samples + the live one
        assert mole.calls == ["P"] and subscribed == ["P"]
        assert test_service.ring_max_points("P") == test_service.max_points

    async def test_scalar_subscribe_latest_ring_then_timeseries_has_archive_history(self, test_service, mole, clock):
        """A size-1 subscribe_latest ring is grown and provisioned for a timeseries reader."""
        mole.history["Q"] = _archive(10, end_ns=NOW - 100 * S)
        source = _source(test_service, mole, clock, [])
        await test_service.seed("Q", [], max_points=1)
        await test_service.append("Q", _sample(NOW - S, 42.0), recv_ns=1)

        window = await source.read_aligned(["Q"], NOW - 3600 * S, NOW, align="last", index="union")
        assert window.ts_ns.size == 11
        # The seeded history is retained: the ring was grown before seeding, so retention did not trim it.
        assert test_service.ring_max_points("Q") == test_service.max_points
        assert test_service.ring_count("Q") == 11

    async def test_refused_pv_costs_one_mole_call_per_retry_period(self, test_service, mole, clock):
        mole.refuse.add("R")
        source = _source(test_service, mole, clock, [])
        for _ in range(10):
            assert await source.read_latest("R") is None
        assert mole.calls.count("R") == 1
        clock.advance(61 * S)
        await source.read_latest("R")
        assert mole.calls.count("R") == 2

    async def test_refusal_before_the_ring_exists_still_defers_once_ingest_creates_it(self, test_service, mole, clock):
        mole.refuse.add("R")
        source = _source(test_service, mole, clock, [])
        with pytest.raises(PVNotBuffered):  # refused and no ring yet: nothing to read
            await source.read_aligned(["R"], NOW - 3600 * S, NOW, align="last", index="union")
        await test_service.append("R", _sample(NOW - S, 1.0), recv_ns=1)  # live ingest creates the ring
        await source.read_aligned(["R"], NOW - 3600 * S, NOW, align="last", index="union")
        assert mole.calls.count("R") == 1
        clock.advance(61 * S)
        await source.read_aligned(["R"], NOW - 3600 * S, NOW, align="last", index="union")
        assert mole.calls.count("R") == 2

    async def test_read_latest_on_a_subscribe_latest_ring_with_a_value_never_fetches(self, test_service, mole, clock):
        """Scalar/waveform inputs: a latest-value read neither calls Mole nor grows the size-1 ring."""
        mole.history["P"] = _archive(900, end_ns=NOW - 10 * S)
        source = _source(test_service, mole, clock, [])
        await test_service.seed("P", [], max_points=1)  # what subscribe_latest creates
        await test_service.append("P", _sample(NOW - S, 42.0), recv_ns=1)

        sample = await source.read_latest("P")
        assert sample is not None and sample.value == 42.0
        assert mole.calls == []
        assert test_service.ring_max_points("P") == 1

    async def test_read_latest_on_an_empty_size_one_ring_provisions_without_growing(self, test_service, mole, clock):
        mole.history["P"] = _archive(900, end_ns=NOW - 10 * S)
        source = _source(test_service, mole, clock, [])
        await test_service.seed("P", [], max_points=1)

        sample = await source.read_latest("P")
        assert sample is not None and sample.value == 899.0
        assert mole.calls == ["P"]
        assert test_service.ring_max_points("P") == 1
        assert test_service.ring_count("P") == 1

    async def test_grace_dropped_ring_reprovisions_on_read_latest(self, test_service, mole, clock):
        mole.history["P"] = _archive(3, end_ns=NOW - 10 * S)
        source = _source(test_service, mole, clock, [])
        assert (await source.read_latest("P")) is not None
        await test_service.drop("P")  # grace expired
        assert (await source.read_latest("P")) is not None
        assert mole.calls == ["P", "P"]

    async def test_provisioned_ring_is_not_fetched_again(self, test_service, mole, clock):
        mole.history["P"] = _archive(3, end_ns=NOW - 10 * S)
        source = _source(test_service, mole, clock, [])
        await source.read_aligned(["P"], NOW - 3600 * S, NOW, align="last", index="union")
        await source.read_aligned(["P"], NOW - 3600 * S, NOW, align="last", index="union")
        await source.read_latest("P")
        assert mole.calls == ["P"]

    async def test_pressure_shrink_clears_provisioned_so_the_next_read_reprovisions(self, mole, clock):
        config = VoleConfig(max_points=1000, global_bytes_max=BYTES_PER_SAMPLE * 500, settle_depth=4)
        service = _service(config, clock)
        mole.history["P"] = _archive(3, end_ns=NOW - 10 * S)
        source = _source(service, mole, clock, [])
        await source.read_aligned(["P"], NOW - 3600 * S, NOW, align="last", index="union")
        service.pin(["P"], holder="t")
        for i in range(1, 601):
            await service.append("P", _sample(NOW + i, float(i)), recv_ns=i)
        assert service.ring_max_points("P") == 4  # shrunk to settle depth under pressure
        await source.read_aligned(["P"], NOW - 3600 * S, NOW, align="last", index="union")
        assert mole.calls == ["P", "P"]

    async def test_numeric_string_pv_timeseries_is_unchanged(self, test_service):
        """Regression pin: alignment_service._to_float still parses numeric strings."""
        await test_service.append("STR", _sample(S, "3.5"))
        await test_service.append("STR", _sample(2 * S, "4.5"))
        result = await test_service.read_aligned(["STR"], 0, 3 * S, align="last", index="union")
        assert result.arrays["STR"].tolist() == [3.5, 4.5]


# --- pinned memory bound ------------------------------------------------------- #


class TestPinnedMemoryBound:
    async def test_all_pinned_over_ceiling_shrinks_largest_to_settle_depth_and_keeps_ingesting(self):
        config = VoleConfig(max_points=1000, global_bytes_max=BYTES_PER_SAMPLE * 30, settle_depth=4)
        store = PVStore(config)
        dropped: list[str] = []

        async def on_evict(pvs):
            dropped.extend(pvs)

        eviction = EvictionService(store, config, on_evict=on_evict)
        service = BufferService(store, AlignmentService(), eviction, FakeClock(start_ns=NOW), config)
        service.pin(["BIG", "SMALL"], holder="t")
        for i in range(1, 7):
            await service.append("SMALL", _sample(i * S), recv_ns=i)
        for i in range(1, 41):
            await service.append("BIG", _sample(i * S), recv_ns=i)  # never raises BufferAtCapacity

        assert dropped == []
        assert store.contains("BIG") and store.contains("SMALL")
        assert service.ring_count("BIG") == 4 and service.ring_max_points("BIG") == 4
        assert service.ring_count("SMALL") == 6  # not the largest: untouched
        assert store.total_bytes <= config.global_bytes_max
        assert service.read_latest_sync("BIG").ts_ns == 40 * S

    async def test_eviction_returns_when_nothing_is_left_to_shrink(self):
        config = VoleConfig(max_points=1000, global_bytes_max=BYTES_PER_SAMPLE * 2, settle_depth=4)
        store = PVStore(config)
        eviction = EvictionService(store, config)
        service = BufferService(store, AlignmentService(), eviction, FakeClock(start_ns=NOW), config)
        service.pin(["A", "B"], holder="t")
        for i in range(1, 5):
            await service.append("A", _sample(i * S), recv_ns=i)
            await service.append("B", _sample(i * S), recv_ns=i)
        assert await asyncio.wait_for(eviction.evict_lru(), 1.0) == []
        assert service.ring_count("A") == 4 and service.ring_count("B") == 4

    async def test_a_stuck_ceiling_skips_rescans_until_something_becomes_relievable(self, monkeypatch):
        """All pinned at settle depth over the ceiling: live appends do not re-sort every ring."""
        config = VoleConfig(max_points=1000, global_bytes_max=BYTES_PER_SAMPLE * 2, settle_depth=4)
        store = PVStore(config)
        eviction = EvictionService(store, config)
        service = BufferService(store, AlignmentService(), eviction, FakeClock(start_ns=NOW), config)
        service.pin(["A", "B"], holder="t")
        for i in range(1, 7):  # over the ceiling, the next passes shrink each ceiling to settle depth
            await service.append("A", _sample(i * S), recv_ns=i)
            await service.append("B", _sample(i * S), recv_ns=i)
        assert service.ring_max_points("A") == 4 and service.ring_max_points("B") == 4
        service.ensure_capacity("A", 4)  # no growth: still stuck
        await eviction.evict_lru()  # records the stuck state

        scans = 0
        real = store.pinned_candidates

        def counting():
            nonlocal scans
            scans += 1
            return real()

        monkeypatch.setattr(store, "pinned_candidates", counting)
        for i in range(5, 25):
            await service.append("A", _sample(i * S), recv_ns=i)
        assert scans == 0

        service.unpin("t")  # A and B become droppable: the next pass runs again
        await service.append("A", _sample(30 * S), recv_ns=30)
        assert store.total_bytes <= config.global_bytes_max
