"""Tests for HareIngestService using a fake broker."""

from canopy.contracts.sample import AlarmSeverity
from canopy.epics.pv_protocol import strip_protocol_prefix
from canopy.vole.services.hare_ingest import HareIngestService


class FakeBroker:
    """Minimal fake implementing the PVBroker client API."""

    def __init__(self):
        self.clients: dict[str, object] = {}
        self.subscriptions: dict[str, list[str]] = {}
        self.unsubscribed: list[list[str]] = []

    def add_client(self, client_id: str, channel) -> None:
        self.clients[client_id] = channel

    async def subscribe(self, client_id: str, pvs: list[str]) -> list[str]:
        self.subscriptions.setdefault(client_id, []).extend(pvs)
        return list(pvs)

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

    async def test_broker_rejected_pv_not_recorded_as_subscribed(self, test_service):
        """A name the broker drops must not enter _subscribed_pvs, or GraceManager
        starts a grace timer for a PV that was never subscribed."""

        class RejectingBroker(FakeBroker):
            async def subscribe(self, client_id: str, pvs: list[str]) -> list[str]:
                accepted = [pv for pv in pvs if pv != "PV:BAD"]
                self.subscriptions.setdefault(client_id, []).extend(accepted)
                return accepted

        ingest = HareIngestService(test_service, RejectingBroker())
        await ingest.start()

        subscribed = await ingest.subscribe(["PV:GOOD", "PV:BAD"])

        assert subscribed == ["PV:GOOD"]
        assert ingest.subscribed_pvs == {"PV:GOOD"}
        await ingest.stop()

    async def test_subscribe_returns_already_subscribed_pvs(self, test_service):
        broker = FakeBroker()
        ingest = HareIngestService(test_service, broker)
        await ingest.start()

        await ingest.subscribe(["PV:A"])
        assert await ingest.subscribe(["PV:A", "PV:B"]) == ["PV:A", "PV:B"]

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
        await channel.send_json(
            {
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
            }
        )

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
        await channel.send_json(
            {
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
            }
        )

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
        await channel.send_json(
            {
                "type": "heartbeat",
                "timestamp": 1234567890,
                "backend": "simulation",
            }
        )
        # No crash, no data appended
        stats = await test_service.stats()
        assert stats.ring_count == 0

        await ingest.stop()

    async def test_disconnected_pv_skipped(self, test_service):
        broker = FakeBroker()
        ingest = HareIngestService(test_service, broker)
        await ingest.start()

        channel = broker.clients[HareIngestService.CLIENT_ID]
        await channel.send_json(
            {
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
            }
        )

        latest = await test_service.read_latest("PV:OFFLINE")
        assert latest is None

        await ingest.stop()

    async def test_malformed_ts_does_not_break_other_pvs(self, test_service):
        # A non-numeric tsNs on one PV must skip only that PV; the rest of the
        # frame still lands and no exception escapes into the broker.
        broker = FakeBroker()
        ingest = HareIngestService(test_service, broker)
        await ingest.start()

        ts = 990_000_000_000
        channel = broker.clients[HareIngestService.CLIENT_ID]
        await channel.send_json(
            {
                "type": "diff",
                "data": {
                    "PV:BAD": {"pv": "PV:BAD", "value": 1.0, "severity": "NO_ALARM", "tsNs": "N/A", "connected": True},
                    "PV:GOOD": {"pv": "PV:GOOD", "value": 2.0, "severity": "NO_ALARM", "tsNs": ts, "connected": True},
                },
                "count": 2,
            }
        )

        assert await test_service.read_latest("PV:BAD") is None
        good = await test_service.read_latest("PV:GOOD")
        assert good is not None and good.value == 2.0
        # Vole is still a registered broker client — ingestion was not torn down.
        assert HareIngestService.CLIENT_ID in broker.clients

        await ingest.stop()

    async def test_send_json_never_raises_on_garbage_frame(self, test_service):
        # send_json is the broker-facing surface; if it raises, the broker
        # unregisters Vole and kills all ingestion. It must swallow anything.
        broker = FakeBroker()
        ingest = HareIngestService(test_service, broker)
        await ingest.start()

        channel = broker.clients[HareIngestService.CLIENT_ID]
        # data is not a dict; tsNs is a dict; value is unrepresentable — none may raise.
        await channel.send_json({"type": "diff", "data": {"PV:X": {"tsNs": {}, "value": 1.0, "connected": True}}})
        await channel.send_json({"type": "diff", "data": "not-a-dict"})
        await channel.send_json("not-a-frame")

        assert HareIngestService.CLIENT_ID in broker.clients
        await ingest.stop()

    async def test_multiple_pvs_in_one_diff(self, test_service):
        broker = FakeBroker()
        ingest = HareIngestService(test_service, broker)
        await ingest.start()

        # Use timestamps within retention window of FakeClock (start=1_000_000_000_000)
        ts = 990_000_000_000

        channel = broker.clients[HareIngestService.CLIENT_ID]
        await channel.send_json(
            {
                "type": "diff",
                "data": {
                    "PV:A": {"pv": "PV:A", "value": 1.0, "severity": "NO_ALARM", "tsNs": ts, "connected": True},
                    "PV:B": {"pv": "PV:B", "value": 2.0, "severity": "MAJOR", "tsNs": ts + 100, "connected": True},
                },
                "count": 2,
            }
        )

        a = await test_service.read_latest("PV:A")
        b = await test_service.read_latest("PV:B")
        assert a is not None and a.value == 1.0
        assert b is not None and b.value == 2.0
        assert b.severity == AlarmSeverity.MAJOR

        await ingest.stop()


class TestProtocolPrefixStripping:
    def test_ca_prefix(self):
        assert strip_protocol_prefix("ca://QUAD:BDES") == "QUAD:BDES"

    def test_pva_prefix(self):
        assert strip_protocol_prefix("pva://TEST:IMAGE") == "TEST:IMAGE"

    def test_no_prefix(self):
        assert strip_protocol_prefix("PV:PLAIN") == "PV:PLAIN"
