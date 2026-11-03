"""Opening and closing an outage tells Vole's subscribers, and the open state can be asked.

The Formula Engine re-evaluates a live formula only when Vole notifies a change on one of its
inputs. A disconnect stores no sample, so without a notification on the outage
itself a formula would keep showing its last value until the PV came back. The
open state is queryable (``BufferService.in_open_outage``) whether or not the PV
had a ring when it died.
"""

from __future__ import annotations

import asyncio

import pytest

from canopy.core.clock import FakeClock
from canopy.vole.config import VoleConfig
from canopy.contracts.sample import Sample, AlarmSeverity
from canopy.vole.buffer.store import PVStore
from canopy.vole.services.notifier import ChangeNotifier
from tests.canopy.vole.test_outages import FakeBroker
from canopy.vole.services.hare_ingest import HareIngestService
from canopy.vole.services.buffer_service import BufferService
from canopy.vole.services.eviction_service import EvictionService
from canopy.vole.services.alignment_service import AlignmentService

S = 1_000_000_000
NOW = 1_000_000_000_000


def _sample(value: float = 1.0) -> Sample:
    return Sample(ts_ns=NOW, value=value, severity=AlarmSeverity.NO_ALARM, source="live")


def _pv(ts_ns: int, value=1.0, *, connected: bool = True) -> dict:
    return {"pv": "P", "value": value, "severity": "NO_ALARM", "tsNs": ts_ns, "connected": connected}


@pytest.fixture
def fake_clock() -> FakeClock:
    return FakeClock(start_ns=NOW)


@pytest.fixture
def notifier() -> ChangeNotifier:
    return ChangeNotifier()


@pytest.fixture
def buffer(fake_clock, notifier) -> BufferService:
    config = VoleConfig(max_points=1000, global_bytes_max=1_000_000)
    store = PVStore(config)
    return BufferService(
        store, AlignmentService(), EvictionService(store, config), fake_clock, config, notifier=notifier
    )


class _Calls:
    def __init__(self) -> None:
        self.pvs: list[str] = []

    async def __call__(self, pv: str) -> None:
        self.pvs.append(pv)


async def _settle() -> None:
    # notify() schedules callbacks as tasks; let them run.
    for _ in range(3):
        await asyncio.sleep(0)


class TestNotify:
    async def test_open_outage_notifies_the_pvs_subscribers(self, buffer, notifier):
        await buffer.append("P", _sample())
        await _settle()
        calls = _Calls()
        notifier.subscribe("P", calls)

        buffer.open_outage("P", NOW + S)
        await _settle()
        assert calls.pvs == ["P"]

    async def test_close_outage_notifies_the_pvs_subscribers(self, buffer, notifier):
        await buffer.append("P", _sample())
        buffer.open_outage("P", NOW + S)
        await _settle()
        calls = _Calls()
        notifier.subscribe("P", calls)

        buffer.close_outage("P", NOW + 2 * S)
        await _settle()
        assert calls.pvs == ["P"]

    async def test_outage_on_a_pv_without_a_ring_notifies(self, buffer, notifier):
        calls = _Calls()
        notifier.subscribe("P", calls)
        buffer.open_outage("P", NOW)
        buffer.close_outage("P", NOW + S)
        await _settle()
        assert calls.pvs == ["P", "P"]

    async def test_only_that_pvs_subscribers_hear(self, buffer, notifier):
        calls_p, calls_q = _Calls(), _Calls()
        notifier.subscribe("P", calls_p)
        notifier.subscribe("Q", calls_q)
        buffer.open_outage("P", NOW)
        await _settle()
        assert calls_p.pvs == ["P"]
        assert calls_q.pvs == []

    def test_a_buffer_without_a_notifier_opens_and_closes_quietly(self, fake_clock):
        config = VoleConfig(max_points=1000, global_bytes_max=1_000_000)
        store = PVStore(config)
        buffer = BufferService(store, AlignmentService(), EvictionService(store, config), fake_clock, config)
        buffer.open_outage("P", NOW)
        assert buffer.in_open_outage("P") is True
        buffer.close_outage("P", NOW + S)
        assert buffer.in_open_outage("P") is False


class TestInOpenOutage:
    async def test_ring_outage_is_open_until_closed(self, buffer):
        await buffer.append("P", _sample())
        assert buffer.in_open_outage("P") is False
        buffer.open_outage("P", NOW + S)
        assert buffer.in_open_outage("P") is True
        buffer.close_outage("P", NOW + 2 * S)
        assert buffer.in_open_outage("P") is False
        assert buffer.outages("P") == [(NOW + S, NOW + 2 * S)]

    def test_pending_outage_before_the_ring_exists_is_open(self, buffer):
        buffer.open_outage("P", NOW)
        assert buffer.in_open_outage("P") is True
        buffer.ensure_capacity("P", 16)  # the ring takes the pending outage over
        assert buffer.in_open_outage("P") is True

    def test_unknown_pv_is_not_in_outage(self, buffer):
        assert buffer.in_open_outage("NOPE") is False


class TestThroughHareIngest:
    @pytest.fixture
    async def channel(self, buffer):
        service = HareIngestService(buffer, FakeBroker())
        await service.start()
        yield service._channel
        await service.stop()

    async def test_disconnect_frame_notifies_and_reconnect_notifies_again(self, buffer, notifier, fake_clock, channel):
        await channel.send_json({"type": "initial", "data": {"P": _pv(NOW)}})
        await _settle()
        calls = _Calls()
        notifier.subscribe("P", calls)

        fake_clock.advance(S)
        await channel.send_json({"type": "diff", "data": {"P": _pv(NOW, None, connected=False)}})
        await _settle()
        assert calls.pvs == ["P"]
        assert buffer.in_open_outage("P") is True

        fake_clock.advance(S)
        await channel.send_json({"type": "diff", "data": {"P": _pv(NOW + 2 * S, 2.0)}})
        await _settle()
        # close_outage notifies, then the reconnect sample's append notifies.
        assert calls.pvs == ["P", "P", "P"]
        assert buffer.in_open_outage("P") is False

    async def test_protocol_prefixed_name_notifies_the_canonical_subscribers(
        self, buffer, notifier, fake_clock, channel
    ):
        calls = _Calls()
        notifier.subscribe("P", calls)
        await channel.send_json({"type": "initial", "data": {"ca://P": _pv(NOW, None, connected=False)}})
        await _settle()
        assert calls.pvs == ["P"]
        assert buffer.in_open_outage("P") is True
