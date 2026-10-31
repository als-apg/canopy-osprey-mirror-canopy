"""Hare ingest: outage intervals, the flush-marker watermark, receive times and ``after_connect``.

Frames are pushed through ``VoleChannel.send_json`` exactly as the broker does,
with a FakeClock standing in for the server's receive clock.
"""

from __future__ import annotations

import time

import pytest

from canopy.contracts.sample import AlarmSeverity
from canopy.vole.buffer.ring import MAX_CLOSED_OUTAGES, PerPVRing, StoredSample
from canopy.vole.services.hare_ingest import HareIngestService
from canopy.vole.services.buffer_service import _RETRY_PRUNE_MIN

S = 1_000_000_000
NOW = 1_000_000_000_000


class FakeBroker:
    """Accepts every subscribe; records the names it was asked for."""

    def __init__(self) -> None:
        self.clients: dict[str, object] = {}
        self.subscribes: list[list[str]] = []

    def add_client(self, client_id: str, channel) -> None:
        self.clients[client_id] = channel

    async def subscribe(self, client_id: str, pvs: list[str]) -> list[str]:
        self.subscribes.append(list(pvs))
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


class TestAliases:
    async def test_unsubscribing_one_alias_mid_outage_keeps_the_outage_closable(
        self, test_service, fake_clock, ingest, channel
    ):
        """``ca://P`` and ``P`` feed one ring; dropping one alias while P is down must not forget the outage."""
        await ingest.subscribe(["ca://P", "P"])
        await channel.send_json({"type": "initial", "data": {"P": _pv(NOW)}})
        fake_clock.advance(S)
        await channel.send_json({"type": "diff", "data": {"P": _pv(NOW, None, connected=False)}})
        assert test_service.in_open_outage("P") is True

        await ingest.unsubscribe(["ca://P"])
        assert ingest.is_subscribed("P") is True

        fake_clock.advance(4 * S)
        await channel.send_json({"type": "diff", "data": {"P": _pv(NOW + 5 * S, 2.0)}})
        assert test_service.in_open_outage("P") is False
        assert test_service.outages("P") == [(NOW + S, NOW + 5 * S)]
        assert test_service.last_at_or_before("P", NOW + 5 * S).after_connect is True

    async def test_unsubscribing_the_last_alias_forgets_the_state(self, test_service, ingest, channel):
        await ingest.subscribe(["ca://P", "P"])
        await channel.send_json({"type": "diff", "data": {"P": _pv(NOW, None, connected=False)}})
        await ingest.unsubscribe(["ca://P", "P"])
        assert ingest.is_subscribed("P") is False
        assert "P" not in ingest._disconnected


class TestSeverity:
    async def test_invalid_severity_is_stored_raw(self, test_service, channel):
        frame = {"type": "diff", "data": {"P": {**_pv(NOW), "severity": "INVALID"}}}
        await channel.send_json(frame)
        assert test_service.last_at_or_before("P", NOW).severity is AlarmSeverity.INVALID


class TestBounds:
    def test_closed_outages_older_than_the_oldest_live_sample_are_dropped(self):
        ring = PerPVRing(max_points=2)
        ring.append(StoredSample(1, 1.0, AlarmSeverity.NO_ALARM, "live", recv_ns=5), now_ns=5)
        ring.open_outage(10)
        ring.close_outage(20)
        ring.append(StoredSample(2, 1.0, AlarmSeverity.NO_ALARM, "live", recv_ns=30), now_ns=30)
        assert ring.outages == [(10, 20)]  # the sample received at 5 is still held

        ring.append(StoredSample(3, 1.0, AlarmSeverity.NO_ALARM, "live", recv_ns=40), now_ns=40)
        assert ring.outages == []  # trimmed to samples received at 30 and 40
        assert ring.in_outage(15) is False

    def test_an_outage_reaching_into_the_retained_samples_is_kept(self):
        ring = PerPVRing(max_points=1)
        ring.append(StoredSample(1, 1.0, AlarmSeverity.NO_ALARM, "live", recv_ns=10), now_ns=10)
        ring.open_outage(20)
        ring.close_outage(40)
        ring.append(StoredSample(2, 1.0, AlarmSeverity.NO_ALARM, "live", recv_ns=40), now_ns=40)
        assert ring.outages == [(20, 40)]

    def test_closed_outages_are_capped(self):
        ring = PerPVRing(max_points=10)
        for i in range(MAX_CLOSED_OUTAGES + 10):
            ring.open_outage(2 * i)
            ring.close_outage(2 * i + 1)
        ring.open_outage(10_000)
        outages = ring.outages
        assert len(outages) == MAX_CLOSED_OUTAGES + 1
        assert outages[0] == (20, 21)  # the oldest ten went first
        assert outages[-1] == (10_000, None)  # the open one is never dropped

    def test_expired_ring_less_provision_deferrals_are_pruned(self, test_service, fake_clock):
        for i in range(_RETRY_PRUNE_MIN - 1):
            test_service.defer_provision(f"OLD:{i}", fake_clock.now_ns() + S)
        fake_clock.advance(2 * S)
        test_service.defer_provision("NEW:1", fake_clock.now_ns() + S)  # reaches the mark: prunes
        assert test_service._retry_after == {"NEW:1": fake_clock.now_ns() + S}
        assert test_service.provision_retry_after_ns("OLD:0") == 0
