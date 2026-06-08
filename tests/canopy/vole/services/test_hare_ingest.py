"""Tests for HareIngestService using a fake broker."""

import pytest

from canopy.contracts.sample import AlarmSeverity
from canopy.vole.services.hare_ingest import HareIngestService, VoleChannel, _strip_protocol_prefix


class FakeBroker:
    """Minimal fake implementing the PVBroker client API."""

    def __init__(self):
        self.clients: dict[str, object] = {}
        self.subscriptions: dict[str, list[str]] = {}
        self.unsubscribed: list[list[str]] = []

    def add_client(self, client_id: str, channel) -> None:
        self.clients[client_id] = channel

    async def subscribe(self, client_id: str, pvs: list[str]) -> None:
        self.subscriptions.setdefault(client_id, []).extend(pvs)

    async def unsubscribe(self, client_id: str, pvs: list[str]) -> None:
        self.unsubscribed.append(pvs)

    async def remove_client(self, client_id: str) -> None:
        self.clients.pop(client_id, None)


class TestHareIngestLifecycle:
    async def test_start_registers_client(self, test_service):
        broker = FakeBroker()
        ingest = HareIngestService(test_service, broker)
        await ingest.start()

        assert HareIngestService.CLIENT_ID in broker.clients
        await ingest.stop()

    async def test_stop_unregisters_client(self, test_service):
        broker = FakeBroker()
        ingest = HareIngestService(test_service, broker)
        await ingest.start()
        await ingest.stop()

        assert HareIngestService.CLIENT_ID not in broker.clients

    async def test_subscribe_adds_pvs(self, test_service):
        broker = FakeBroker()
        ingest = HareIngestService(test_service, broker)
        await ingest.start()

        await ingest.subscribe(["ca://PV:A", "pva://PV:B"])
        assert "ca://PV:A" in ingest.subscribed_pvs
        assert "pva://PV:B" in ingest.subscribed_pvs
        assert broker.subscriptions[HareIngestService.CLIENT_ID] == ["ca://PV:A", "pva://PV:B"]

        await ingest.stop()

    async def test_unsubscribe_removes_pvs(self, test_service):
        broker = FakeBroker()
        ingest = HareIngestService(test_service, broker)
        await ingest.start()
        await ingest.subscribe(["ca://PV:A", "ca://PV:B"])
        await ingest.unsubscribe(["ca://PV:A"])

        assert "ca://PV:A" not in ingest.subscribed_pvs
        assert "ca://PV:B" in ingest.subscribed_pvs
        await ingest.stop()

    async def test_duplicate_subscribe_ignored(self, test_service):
        broker = FakeBroker()
        ingest = HareIngestService(test_service, broker)
        await ingest.start()

        await ingest.subscribe(["ca://PV:A"])
        await ingest.subscribe(["ca://PV:A"])
        # Only one subscribe call to broker
        assert broker.subscriptions[HareIngestService.CLIENT_ID] == ["ca://PV:A"]

        await ingest.stop()


class TestDiffHandling:
    async def test_diff_appends_to_ring(self, test_service):
        broker = FakeBroker()
        ingest = HareIngestService(test_service, broker)
        await ingest.start()

        channel = broker.clients[HareIngestService.CLIENT_ID]
        await channel.send_json({
            "type": "diff",
            "data": {
                "ca://PV:TEMP": {
                    "pv": "ca://PV:TEMP",
                    "value": 25.5,
                    "severity": "NO_ALARM",
                    "tsNs": 1000000000000,
                    "connected": True,
                },
            },
            "count": 1,
        })

        latest = await test_service.read_latest("PV:TEMP")
        assert latest is not None
        assert latest.value == 25.5
        assert latest.ts_ns == 1000000000000
        assert latest.source == "live"
        assert latest.severity == AlarmSeverity.NO_ALARM

        await ingest.stop()

    async def test_initial_frame_appends(self, test_service):
        broker = FakeBroker()
        ingest = HareIngestService(test_service, broker)
        await ingest.start()

        channel = broker.clients[HareIngestService.CLIENT_ID]
        await channel.send_json({
            "type": "initial",
            "data": {
                "pva://QUAD:BDES": {
                    "pv": "pva://QUAD:BDES",
                    "value": 3.14,
                    "severity": "MINOR",
                    "tsNs": 2000000000000,
                    "connected": True,
                },
            },
            "count": 1,
        })

        latest = await test_service.read_latest("QUAD:BDES")
        assert latest is not None
        assert latest.value == 3.14
        assert latest.severity == AlarmSeverity.MINOR

        await ingest.stop()

    async def test_heartbeat_ignored(self, test_service):
        broker = FakeBroker()
        ingest = HareIngestService(test_service, broker)
        await ingest.start()

        channel = broker.clients[HareIngestService.CLIENT_ID]
        await channel.send_json({
            "type": "heartbeat",
            "timestamp": 1234567890,
            "backend": "simulation",
        })
        # No crash, no data appended
        stats = await test_service.stats()
        assert stats.ring_count == 0

        await ingest.stop()

    async def test_disconnected_pv_skipped(self, test_service):
        broker = FakeBroker()
        ingest = HareIngestService(test_service, broker)
        await ingest.start()

        channel = broker.clients[HareIngestService.CLIENT_ID]
        await channel.send_json({
            "type": "diff",
            "data": {
                "ca://PV:OFFLINE": {
                    "pv": "ca://PV:OFFLINE",
                    "value": None,
                    "severity": "INVALID",
                    "tsNs": 1000000000000,
                    "connected": False,
                    "error": "Disconnected",
                },
            },
            "count": 1,
        })

        latest = await test_service.read_latest("PV:OFFLINE")
        assert latest is None

        await ingest.stop()

    async def test_multiple_pvs_in_one_diff(self, test_service):
        broker = FakeBroker()
        ingest = HareIngestService(test_service, broker)
        await ingest.start()

        # Use timestamps within retention window of FakeClock (start=1_000_000_000_000)
        ts = 990_000_000_000

        channel = broker.clients[HareIngestService.CLIENT_ID]
        await channel.send_json({
            "type": "diff",
            "data": {
                "PV:A": {"pv": "PV:A", "value": 1.0, "severity": "NO_ALARM", "tsNs": ts, "connected": True},
                "PV:B": {"pv": "PV:B", "value": 2.0, "severity": "MAJOR", "tsNs": ts + 100, "connected": True},
            },
            "count": 2,
        })

        a = await test_service.read_latest("PV:A")
        b = await test_service.read_latest("PV:B")
        assert a is not None and a.value == 1.0
        assert b is not None and b.value == 2.0
        assert b.severity == AlarmSeverity.MAJOR

        await ingest.stop()


class TestProtocolPrefixStripping:
    def test_ca_prefix(self):
        assert _strip_protocol_prefix("ca://QUAD:BDES") == "QUAD:BDES"

    def test_pva_prefix(self):
        assert _strip_protocol_prefix("pva://TEST:IMAGE") == "TEST:IMAGE"

    def test_no_prefix(self):
        assert _strip_protocol_prefix("PV:PLAIN") == "PV:PLAIN"
