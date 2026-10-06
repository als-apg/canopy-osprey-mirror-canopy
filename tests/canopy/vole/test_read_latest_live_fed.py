"""A live-fed input never falls back to the archive, and ring keys are canonical.

Fox subscribes a live formula's inputs without waiting for the broker's seed, so
the first evaluate can run on an empty ring. ``VoleDataSource.read_latest`` must
then read the ring as it is for a name Vole's ingest is subscribed to -- no Mole
round trip, no archived value served as live, a dead archived name reported as
missing -- while a name nobody live-feeds still provisions from Mole.

The predicate is derived from the ingest's subscriptions, so it holds only while
every dropped ring also means an unsubscribed name: a ``ca://X`` binding seeds
ring ``X``, and evicting or expiring ring ``X`` unsubscribes ``ca://X``.
"""

from __future__ import annotations

import asyncio
from dataclasses import field, dataclass

import pytest

from canopy.hare import gateway as hare_gateway
from canopy.vole import app as vole_app_module
from canopy.vole import gateway
from canopy.core.clock import FakeClock
from canopy.fox.config import FoxConfig
from canopy.fox.errors import FormulaInputMissing
from canopy.hare.config import HareConfig
from canopy.vole.config import VoleConfig
from canopy.contracts.pv import PVMeta, PVValue, ValueKind
from canopy.vole.adapter import VoleDataSource
from canopy.contracts.sample import Sample, AlarmSeverity
from canopy.contracts.formula import FormulaSpec
from canopy.vole.buffer.store import PVStore
from canopy.fox.services.parser import Parser
from tests.canopy.hare.conftest import FakeConnector
from canopy.hare.services.broker import PVBroker
from canopy.vole.services.notifier import ChangeNotifier
from canopy.vole.services.hare_ingest import HareIngestService
from canopy.vole.services.grace_manager import GraceManager
from canopy.fox.services.formula_service import FormulaService
from canopy.vole.services.buffer_service import BufferService
from canopy.vole.services.eviction_service import EvictionService
from canopy.vole.services.alignment_service import AlignmentService

S = 1_000_000_000
NOW = 1_000_000_000_000
SEED_DELAY_S = 30.0  # a pending seed: the broker's read has not answered during the test


@dataclass
class PendingConnector(FakeConnector):
    """Reads of names in ``pending`` never answer within a test; the rest answer at once."""

    pending: set[str] = field(default_factory=set)
    gets: list[str] = field(default_factory=list)

    async def get(self, pv: str, *, timeout: float | None = None) -> PVValue:
        self.gets.append(pv)
        if pv in self.pending:
            await asyncio.sleep(SEED_DELAY_S)
        return PVValue(pv=pv, value=7.0, severity=AlarmSeverity.NO_ALARM, ts_ns=NOW, connected=True)

    async def describe(self, pv: str) -> PVMeta:
        if pv in self.pending:
            await asyncio.sleep(SEED_DELAY_S)
        return PVMeta(pv=pv, value_kind=ValueKind.SCALAR)


class RecordingMole:
    """``fetch_window`` that records every call and serves an archived value for any name."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def fetch_window(self, pv, t0_ns, t1_ns, *, max_points=None):
        self.calls.append(pv)
        return [Sample(ts_ns=NOW - 3600 * S, value=-99.0, severity=AlarmSeverity.NO_ALARM, source="archive")]


def _buffer(config: VoleConfig, clock: FakeClock, *, on_evict=None) -> BufferService:
    store = PVStore(config)
    eviction = EvictionService(store, config, on_evict=on_evict)
    return BufferService(store, AlignmentService(), eviction, clock, config, notifier=ChangeNotifier())


@pytest.fixture
async def stack():
    """A real broker (pending seeds), ingest and buffer, bound to the Vole gateway."""
    connector = PendingConnector(pending={"DEAD:1", "DEAD:2", "LIVE:1"})
    broker = PVBroker(connector, HareConfig(ws_batch_interval_ms=10, heartbeat_interval_s=100.0))
    config = VoleConfig(max_points=1000, global_bytes_max=1_000_000, buffer_grace_seconds=0)
    buffer = _buffer(config, FakeClock(start_ns=NOW))
    ingest = HareIngestService(buffer, broker)
    await ingest.start()
    gateway.bind(buffer)
    gateway.bind_ingest(ingest)
    try:
        yield buffer, ingest, broker, connector, config
    finally:
        gateway.bind_ingest(None)
        gateway.bind(None)
        await ingest.stop()
        await broker.stop()


def _source(buffer: BufferService, mole: RecordingMole, *, is_live_fed=None, subscribe_live=None) -> VoleDataSource:
    return VoleDataSource(
        buffer,
        fetch_window=mole.fetch_window,
        subscribe_live=subscribe_live or gateway.subscribe_live,
        clock=FakeClock(start_ns=NOW),
        is_live_fed=is_live_fed,
    )


class TestLiveFedReadLatest:
    async def test_a_live_fed_name_makes_no_fetch_call(self, stack):
        buffer, ingest, *_ = stack
        mole = RecordingMole()
        source = _source(buffer, mole, is_live_fed=ingest.is_subscribed)

        await gateway.subscribe_latest(["LIVE:1"], wait_seed=False)
        assert await source.read_latest("LIVE:1") is None  # the seed is still on its way
        assert mole.calls == []
        assert not buffer.is_provisioned("LIVE:1")  # never marked: it dies with the subscription

    async def test_a_dead_archived_name_is_missing_not_the_archived_value(self, stack):
        buffer, ingest, *_ = stack
        mole = RecordingMole()
        source = _source(buffer, mole, is_live_fed=ingest.is_subscribed)
        service = FormulaService(source, Parser(), FoxConfig())
        spec = FormulaSpec(kind="scalar", expression="{a}", bindings={"a": "ca://DEAD:1"})

        await gateway.subscribe_latest(["ca://DEAD:1"], wait_seed=False)
        with pytest.raises(FormulaInputMissing):
            await service.evaluate(spec, t0_ns=0, t1_ns=0)
        assert mole.calls == []

    async def test_live_branch_without_a_ring_reads_nothing_from_the_archive(self, stack):
        buffer, ingest, *_ = stack
        mole = RecordingMole()
        source = _source(buffer, mole, is_live_fed=ingest.is_subscribed)

        await gateway.subscribe_live(["DEAD:2"], wait_seed=False)
        assert await source.read_latest("DEAD:2") is None
        assert mole.calls == []

    async def test_a_non_live_fed_empty_ring_still_provisions(self, stack):
        buffer, ingest, *_ = stack
        mole = RecordingMole()
        subscribed: list[str] = []

        async def subscribe_live(pvs, *, wait_seed=True):
            subscribed.extend(pvs)  # recorded only, so the archived seed is what the read sees

        source = _source(buffer, mole, is_live_fed=ingest.is_subscribed, subscribe_live=subscribe_live)
        await buffer.seed("ARCH:1", [], max_points=1)

        sample = await source.read_latest("ARCH:1")
        assert sample is not None and sample.value == -99.0
        assert mole.calls == ["ARCH:1"] and subscribed == ["ARCH:1"]

    async def test_the_default_predicate_keeps_todays_provisioning(self, stack):
        buffer, *_ = stack
        mole = RecordingMole()
        source = _source(buffer, mole)  # no predicate: nothing is live-fed

        await gateway.subscribe_latest(["LIVE:1"], wait_seed=False)
        sample = await source.read_latest("LIVE:1")
        assert sample is not None and sample.value == -99.0
        assert mole.calls == ["LIVE:1"]

    async def test_a_name_the_broker_rejected_still_falls_back_to_mole(self, stack):
        buffer, ingest, *_ = stack
        mole = RecordingMole()
        source = _source(buffer, mole, is_live_fed=ingest.is_subscribed)

        await gateway.subscribe_latest(["bad name"], wait_seed=False)  # whitespace: the broker drops it
        assert not ingest.is_subscribed("bad name")
        sample = await source.read_latest("bad name")
        assert sample is not None and sample.value == -99.0
        assert mole.calls == ["bad name"]


class TestCanonicalKeys:
    async def test_is_subscribed_matches_on_the_canonical_name(self, stack):
        _buffer_, ingest, *_ = stack
        await ingest.subscribe(["ca://X:1"], wait_seed=False)
        assert ingest.is_subscribed("X:1")
        assert ingest.is_subscribed("ca://X:1")
        assert ingest.is_subscribed("pva://X:1")
        assert not ingest.is_subscribed("X:2")
        await ingest.unsubscribe(["ca://X:1"])
        assert not ingest.is_subscribed("X:1")

    async def test_a_ca_binding_seeds_the_canonical_ring_only(self, stack):
        buffer, *_ = stack
        await gateway.subscribe_latest(["ca://X:1"], wait_seed=False)
        assert buffer.ring_max_points("X:1") == 1
        assert "ca://X:1" not in buffer._store._rings

    async def test_evicting_ring_x_unsubscribes_ca_x_and_a_later_subscribe_gets_a_fresh_seed(self):
        """Through Vole's own lifespan, so the eviction callback under test is the one it wires."""
        connector = PendingConnector()
        broker = PVBroker(connector, HareConfig(ws_batch_interval_ms=10, heartbeat_interval_s=100.0))
        hare_gateway.bind(broker)
        try:
            async with vole_app_module.vole_lifespan():
                ingest = gateway.get_ingest()
                service = gateway.get_service()
                await gateway.subscribe_latest(["ca://X:1"])  # waits for the seed: ring X holds it
                assert (await service.read_latest("X:1")).value == 7.0

                dropped = await service._eviction.evict_lru(target_bytes=0)
                assert "X:1" in dropped
                assert not ingest.is_subscribed("X:1")
                assert "ca://X:1" not in ingest.subscribed_pvs

                # Ring X was dropped with its value: only a fresh broker seed can refill it.
                await gateway.subscribe_latest(["ca://X:1"])
                assert ingest.is_subscribed("X:1")
                latest = await service.read_latest("X:1")
                assert latest is not None and latest.value == 7.0
        finally:
            hare_gateway.bind(None)
            await broker.stop()

    async def test_unsubscribe_canonical_drops_every_alias(self, stack):
        _buffer_, ingest, *_ = stack
        await ingest.subscribe(["ca://X:1", "X:1", "Y:1"], wait_seed=False)
        await ingest.unsubscribe_canonical(["X:1"])
        assert ingest.subscribed_pvs == {"Y:1"}
        assert not ingest.is_subscribed("X:1")

    async def test_grace_expiry_of_ca_x_drops_ring_x(self, stack):
        buffer, ingest, _broker, _connector, config = stack
        await gateway.subscribe_latest(["ca://X:1"], wait_seed=False)
        await buffer.append("X:1", Sample(ts_ns=NOW, value=1.0, severity=AlarmSeverity.NO_ALARM, source="live"))
        grace = GraceManager(ingest, buffer, config)

        await grace.on_pv_deactivated(["ca://X:1"])
        for _ in range(50):
            if not grace._pending:
                break
            await asyncio.sleep(0.01)
        assert not ingest.is_subscribed("X:1")
        assert await buffer.read_latest("X:1") is None
        assert "X:1" not in buffer._store._rings
        await grace.shutdown()

    async def test_grace_expiry_keeps_the_ring_while_another_alias_still_feeds_it(self, stack):
        buffer, ingest, _broker, _connector, config = stack
        await ingest.subscribe(["ca://X:1", "X:1"], wait_seed=False)
        await buffer.append("X:1", Sample(ts_ns=NOW, value=1.0, severity=AlarmSeverity.NO_ALARM, source="live"))
        grace = GraceManager(ingest, buffer, config)

        await grace.on_pv_deactivated(["ca://X:1"])
        for _ in range(50):
            if not grace._pending:
                break
            await asyncio.sleep(0.01)
        assert ingest.subscribed_pvs == {"X:1"}
        assert (await buffer.read_latest("X:1")).value == 1.0
        await grace.shutdown()


class TestDebugEvict:
    async def test_the_debug_evict_route_unsubscribes_every_alias_of_the_dropped_ring(self, stack):
        from canopy.vole.api.v1.debug import evict_pv

        buffer, ingest, *_ = stack
        await ingest.subscribe(["ca://X:1", "X:1", "Y:1"], wait_seed=False)
        await buffer.append("X:1", Sample(ts_ns=NOW, value=1.0, severity=AlarmSeverity.NO_ALARM, source="live"))

        body = await evict_pv("ca://X:1", buffer, ingest)
        assert body == {"status": "dropped", "pv": "ca://X:1"}
        assert ingest.subscribed_pvs == {"Y:1"}
        assert "X:1" not in buffer._store._rings


class TestFoxWiring:
    async def test_fox_predicate_follows_the_bound_ingest_and_is_false_without_one(self, stack):
        from canopy.fox.app import _is_live_fed

        _buffer_, ingest, *_ = stack
        await ingest.subscribe(["ca://X:1"], wait_seed=False)
        assert _is_live_fed("X:1")
        await ingest.unsubscribe(["ca://X:1"])
        assert not _is_live_fed("X:1")  # derived per call: never stale after an unsubscribe
        gateway.bind_ingest(None)
        assert not _is_live_fed("X:1")
