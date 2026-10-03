"""Hare ingest: outage intervals, the flush-marker watermark, receive times and ``after_connect``.

Frames are pushed through ``VoleChannel.send_json`` exactly as the broker does,
with a FakeClock standing in for the server's receive clock (r4:CC-5, b6:IA-13,
b7:CC-1, b7:CF-1).
"""

from __future__ import annotations

import time

import pytest

from canopy.hare.config import HareConfig
from canopy.contracts.sample import AlarmSeverity
from canopy.hare.services.broker import PVBroker
from canopy.vole.services.hare_ingest import HareIngestService
from tests.canopy.hare.test_broker_seeding import DEAD_DELAY_S, SlowConnector

S = 1_000_000_000
NOW = 1_000_000_000_000


class FakeBroker:
    """Records ``subscribe`` calls with their ``wait_seed`` keyword."""

    def __init__(self) -> None:
        self.clients: dict[str, object] = {}
        self.subscribes: list[tuple[list[str], bool | None]] = []

    def add_client(self, client_id: str, channel) -> None:
        self.clients[client_id] = channel

    async def subscribe(self, client_id: str, pvs: list[str], **kwargs) -> list[str]:
        self.subscribes.append((list(pvs), kwargs.get("wait_seed")))
        return list(pvs)

    async def unsubscribe(self, client_id: str, pvs: list[str]) -> None:
        pass

    async def remove_client(self, client_id: str) -> None:
        self.clients.pop(client_id, None)


def _pv(ts_ns: int, value=1.0, *, connected: bool = True) -> dict:
    return {"pv": "P", "value": value, "severity": "NO_ALARM", "tsNs": ts_ns, "connected": connected}


@pytest.fixture
async def ingest(test_service):
    broker = FakeBroker()
    service = HareIngestService(test_service, broker)
    await service.start()
    yield service
    await service.stop()


@pytest.fixture
def channel(ingest):
    return ingest._channel


class TestOutages:
    async def test_disconnect_then_reconnect_records_a_closed_interval_on_the_receive_clock(
        self, test_service, fake_clock, channel
    ):
        """A slow PV with an old IOC timestamp: the interval is stamped with receive time, not tsNs."""
        await channel.send_json({"type": "initial", "data": {"P": _pv(NOW - 3600 * S)}})
        fake_clock.advance(10 * S)
        await channel.send_json({"type": "diff", "data": {"P": _pv(NOW - 3600 * S, None, connected=False)}})
        fake_clock.advance(5 * S)
        await channel.send_json({"type": "diff", "data": {"P": _pv(NOW - 3500 * S, 2.0)}})

        assert test_service.outages("P") == [(NOW + 10 * S, NOW + 15 * S)]
        assert test_service.in_outage("P", NOW + 12 * S) is True
        assert test_service.in_outage("P", NOW + 16 * S) is False

    async def test_open_outage_has_no_end(self, test_service, fake_clock, channel):
        await channel.send_json({"type": "initial", "data": {"P": _pv(NOW)}})
        fake_clock.advance(S)
        await channel.send_json({"type": "diff", "data": {"P": _pv(NOW, None, connected=False)}})
        assert test_service.outages("P") == [(NOW + S, None)]
        assert test_service.in_outage("P", NOW + 100 * S) is True

    async def test_a_pv_dead_before_its_ring_exists_keeps_its_outage(self, test_service, fake_clock, channel):
        await channel.send_json({"type": "initial", "data": {"P": _pv(NOW, None, connected=False)}})
        assert test_service.in_outage("P", NOW + S) is False  # no ring yet: nothing to report on
        test_service.ensure_capacity("P", 16)  # a table input creates the ring mid-outage
        assert test_service.outages("P") == [(NOW, None)]
        fake_clock.advance(7 * S)
        await channel.send_json({"type": "diff", "data": {"P": _pv(NOW + 7 * S, 2.0)}})
        assert test_service.outages("P") == [(NOW, NOW + 7 * S)]

    async def test_outage_spanning_ring_creation_by_the_reconnect_sample_is_kept(
        self, test_service, fake_clock, channel
    ):
        await channel.send_json({"type": "initial", "data": {"P": _pv(NOW, None, connected=False)}})
        fake_clock.advance(3 * S)
        await channel.send_json({"type": "diff", "data": {"P": _pv(NOW + 3 * S, 2.0)}})
        assert test_service.outages("P") == [(NOW, NOW + 3 * S)]
        assert test_service.read_latest_sync("P").value == 2.0

    async def test_disconnected_entries_store_no_sample(self, test_service, channel):
        await channel.send_json({"type": "initial", "data": {"P": _pv(NOW, None, connected=False)}})
        assert test_service.read_latest_sync("P") is None


class TestWatermark:
    async def test_watermark_starts_at_zero_and_follows_flush_markers_only(self, ingest, fake_clock, channel):
        assert ingest.watermark_ns == 0
        await channel.send_json({"type": "diff", "data": {"P": _pv(NOW)}})
        assert ingest.watermark_ns == 0  # a diff alone does not move it
        fake_clock.advance(S)
        await channel.send_json({"type": "flush", "timestamp": time.time()})
        assert ingest.watermark_ns == NOW + S

    async def test_watermark_advances_on_an_empty_flush_with_no_pv_changing(self, ingest, fake_clock, channel):
        await channel.send_json({"type": "flush", "timestamp": time.time()})
        first = ingest.watermark_ns
        fake_clock.advance(S)
        await channel.send_json({"type": "flush", "timestamp": time.time()})
        assert ingest.watermark_ns == first + S

    async def test_heartbeat_does_not_move_the_watermark(self, ingest, fake_clock, channel):
        fake_clock.advance(S)
        await channel.send_json({"type": "heartbeat", "timestamp": time.time()})
        assert ingest.watermark_ns == 0

    async def test_samples_carry_the_receive_time(self, test_service, fake_clock, channel):
        fake_clock.advance(7 * S)
        await channel.send_json({"type": "diff", "data": {"P": _pv(NOW - 100 * S)}})
        stored = test_service.last_at_or_before("P", NOW)
        assert stored.recv_ns == NOW + 7 * S
        assert stored.ts_ns == NOW - 100 * S


class TestAfterConnect:
    async def test_initial_frame_sample_is_after_connect(self, test_service, channel):
        await channel.send_json({"type": "initial", "data": {"P": _pv(NOW)}})
        assert test_service.last_at_or_before("P", NOW).after_connect is True

    async def test_first_sample_at_all_is_after_connect_even_from_a_diff(self, test_service, channel):
        await channel.send_json({"type": "diff", "data": {"P": _pv(NOW)}})
        assert test_service.last_at_or_before("P", NOW).after_connect is True

    async def test_first_diff_after_a_disconnected_entry_is_after_connect(self, test_service, channel):
        await channel.send_json({"type": "initial", "data": {"P": _pv(NOW)}})
        await channel.send_json({"type": "diff", "data": {"P": _pv(NOW + S, None, connected=False)}})
        await channel.send_json({"type": "diff", "data": {"P": _pv(NOW + 2 * S, 2.0)}})
        assert test_service.last_at_or_before("P", NOW + 2 * S).after_connect is True

    async def test_later_diff_samples_are_not_after_connect(self, test_service, channel):
        await channel.send_json({"type": "initial", "data": {"P": _pv(NOW)}})
        await channel.send_json({"type": "diff", "data": {"P": _pv(NOW + S, 2.0)}})
        await channel.send_json({"type": "diff", "data": {"P": _pv(NOW + 2 * S, 3.0)}})
        assert test_service.last_at_or_before("P", NOW + S).after_connect is False
        assert test_service.last_at_or_before("P", NOW + 2 * S).after_connect is False


class TestWaitSeed:
    async def test_default_waits_and_false_passes_through(self, test_service):
        broker = FakeBroker()
        ingest = HareIngestService(test_service, broker)
        await ingest.start()
        await ingest.subscribe(["A"])
        await ingest.subscribe(["B"], wait_seed=False)
        await ingest.subscribe(["C"], wait_seed=True)
        # The default is the broker's own default, passed as no keyword so older fakes keep working.
        assert broker.subscribes == [(["A"], None), (["B"], False), (["C"], None)]

    async def test_subscribe_live_dead_pv_without_waiting_returns_before_the_timeout(self, test_service):
        connector = SlowConnector(dead={"DEAD:1", "DEAD:2"})
        broker = PVBroker(connector, HareConfig(ws_batch_interval_ms=10, heartbeat_interval_s=100.0))
        ingest = HareIngestService(test_service, broker)
        await ingest.start()
        try:
            t0 = time.monotonic()
            assert await ingest.subscribe(["DEAD:1"], wait_seed=False) == ["DEAD:1"]
            assert time.monotonic() - t0 < DEAD_DELAY_S / 2

            t0 = time.monotonic()
            await ingest.subscribe(["DEAD:2"])  # the default still waits for the seed
            assert time.monotonic() - t0 >= DEAD_DELAY_S * 0.9
        finally:
            await ingest.stop()
            await broker.stop()


class TestSeverity:
    async def test_invalid_severity_is_stored_raw(self, test_service, channel):
        frame = {"type": "diff", "data": {"P": {**_pv(NOW), "severity": "INVALID"}}}
        await channel.send_json(frame)
        assert test_service.last_at_or_before("P", NOW).severity is AlarmSeverity.INVALID
