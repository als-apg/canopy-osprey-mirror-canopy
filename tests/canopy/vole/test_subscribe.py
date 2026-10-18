"""Tests for the Vole PV subscribe endpoint and GraceManager lifecycle."""

import asyncio

import pytest
from fastapi.testclient import TestClient

from tests.conftest import READ_HEADERS, wait_for
from canopy.vole.config import VoleConfig
from canopy.vole.services.grace_manager import GraceManager


class FakeIngest:
    """Minimal stand-in for HareIngestService for GraceManager tests."""

    def __init__(self, subscribed: set[str] | None = None) -> None:
        self._subscribed_pvs: set[str] = subscribed or set()
        self.unsubscribed: list[list[str]] = []

    async def subscribe(self, pvs: list[str]) -> list[str]:
        self._subscribed_pvs.update(pvs)
        return list(pvs)

    async def unsubscribe(self, pvs: list[str]) -> None:
        self._subscribed_pvs -= set(pvs)
        self.unsubscribed.append(pvs)

    @property
    def subscribed_pvs(self) -> set[str]:
        return set(self._subscribed_pvs)


class FakeBuffer:
    """Minimal stand-in for BufferService for GraceManager tests."""

    def __init__(self) -> None:
        self.dropped: list[str] = []

    async def drop(self, pv: str) -> None:
        self.dropped.append(pv)


class TestSubscribeEndpoint:
    async def test_subscribe_returns_200(self, vole_client: TestClient) -> None:
        resp = vole_client.post(
            "/vole/v1/pvs/subscribe",
            json={"pvs": ["PV:A", "PV:B"]},
            headers=READ_HEADERS,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["subscribed"] == ["PV:A", "PV:B"]

    async def test_subscribe_empty_pvs_returns_400(self, vole_client: TestClient) -> None:
        resp = vole_client.post(
            "/vole/v1/pvs/subscribe",
            json={"pvs": []},
            headers=READ_HEADERS,
        )
        assert resp.status_code == 400

    async def test_subscribe_no_auth_returns_401(self, vole_client: TestClient) -> None:
        resp = vole_client.post(
            "/vole/v1/pvs/subscribe",
            json={"pvs": ["PV:A"]},
        )
        assert resp.status_code == 401

    async def test_single_subscribe_returns_200(self, vole_client: TestClient) -> None:
        resp = vole_client.post(
            "/vole/v1/pvs/subscribe/PV:A",
            headers=READ_HEADERS,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["subscribed"] == ["PV:A"]

    async def test_subscribe_invalid_pv_name_returns_400(self, vole_client: TestClient, fake_ingest) -> None:
        resp = vole_client.post(
            "/vole/v1/pvs/subscribe",
            json={"pvs": ["PV:A", "has space"]},
            headers=READ_HEADERS,
        )
        assert resp.status_code == 400
        assert resp.json()["code"] == "invalid_pv_name"
        # Rejected before the broker is touched, so the whole batch is a no-op.
        assert fake_ingest.subscribed_pvs == set()

    async def test_single_subscribe_invalid_pv_name_returns_400(self, vole_client: TestClient) -> None:
        resp = vole_client.post(
            "/vole/v1/pvs/subscribe/pva://ca://PV:A",
            headers=READ_HEADERS,
        )
        assert resp.status_code == 400
        assert resp.json()["code"] == "invalid_pv_name"

    async def test_subscribe_reports_only_pvs_ingest_accepted(self, vole_client: TestClient, fake_ingest) -> None:
        """The response reflects the outcome, not the request: a PV the broker
        refused must not be reported as subscribed."""
        fake_ingest.reject = {"PV:B"}

        resp = vole_client.post(
            "/vole/v1/pvs/subscribe",
            json={"pvs": ["PV:A", "PV:B"]},
            headers=READ_HEADERS,
        )
        assert resp.status_code == 200
        assert resp.json()["subscribed"] == ["PV:A"]

    async def test_subscribe_returns_503_when_ingest_unavailable(self) -> None:
        """In Mole-only mode (no Hare) the endpoint is unavailable, not broken."""
        from canopy.vole import gateway
        from canopy.vole.app import app as vole_app
        from canopy.vole.dependencies import get_ingest_service

        vole_app.dependency_overrides.pop(get_ingest_service, None)
        gateway.bind_ingest(None)
        try:
            with TestClient(vole_app) as client:
                resp = client.post("/v1/pvs/subscribe", json={"pvs": ["PV:A"]}, headers=READ_HEADERS)
            assert resp.status_code == 503
            assert resp.json()["code"] == "ingest_unavailable"
        finally:
            gateway.bind_ingest(None)


class TestGraceManager:
    @pytest.fixture
    def grace_config(self) -> VoleConfig:
        return VoleConfig(
            max_points=100,
            global_bytes_max=1_000_000,
            buffer_grace_seconds=0,
        )

    async def test_deactivation_fires_unsubscribe_and_drops_buffer(self, grace_config: VoleConfig) -> None:
        grace_config.buffer_grace_seconds = 0
        ingest = FakeIngest(subscribed={"PV:A"})
        buffer = FakeBuffer()
        mgr = GraceManager(ingest, buffer, grace_config)

        await mgr.on_pv_deactivated(["PV:A"])

        assert await wait_for(lambda: buffer.dropped)
        assert ["PV:A"] in ingest.unsubscribed
        assert buffer.dropped == ["PV:A"]
        await mgr.shutdown()

    async def test_activation_cancels_pending_grace(self, grace_config: VoleConfig) -> None:
        grace_config.buffer_grace_seconds = 10
        ingest = FakeIngest(subscribed={"PV:A"})
        mgr = GraceManager(ingest, FakeBuffer(), grace_config)

        await mgr.on_pv_deactivated(["PV:A"])
        assert "PV:A" in mgr._pending

        await mgr.on_pv_activated(["PV:A"])
        assert "PV:A" not in mgr._pending
        assert ingest.unsubscribed == []
        await mgr.shutdown()

    async def test_deactivation_ignores_pv_not_subscribed(self, grace_config: VoleConfig) -> None:
        ingest = FakeIngest(subscribed=set())
        mgr = GraceManager(ingest, FakeBuffer(), grace_config)

        await mgr.on_pv_deactivated(["PV:NOPE"])
        assert mgr._pending == {}
        await mgr.shutdown()

    async def test_shutdown_cancels_pending_timers(self, grace_config: VoleConfig) -> None:
        grace_config.buffer_grace_seconds = 100
        ingest = FakeIngest(subscribed={"PV:A", "PV:B"})
        mgr = GraceManager(ingest, FakeBuffer(), grace_config)

        await mgr.on_pv_deactivated(["PV:A", "PV:B"])
        assert len(mgr._pending) == 2

        await mgr.shutdown()
        assert mgr._pending == {}
        assert ingest.unsubscribed == []

    async def test_teardown_failure_does_not_leave_pv_pending(self, grace_config: VoleConfig) -> None:
        """A raising unsubscribe must not wedge the PV in _pending forever."""
        grace_config.buffer_grace_seconds = 0

        class FailingIngest(FakeIngest):
            async def unsubscribe(self, pvs: list[str]) -> None:
                raise RuntimeError("broker down")

        mgr = GraceManager(FailingIngest(subscribed={"PV:A"}), FakeBuffer(), grace_config)

        await mgr.on_pv_deactivated(["PV:A"])

        assert await wait_for(lambda: mgr._pending == {})
        await mgr.shutdown()

    async def test_reactivation_during_teardown_cancels_drop(self, grace_config: VoleConfig) -> None:
        """A client reconnecting while unsubscribe is in flight keeps its buffer."""
        grace_config.buffer_grace_seconds = 0
        release = asyncio.Event()
        entered = asyncio.Event()

        class SlowIngest(FakeIngest):
            async def unsubscribe(self, pvs: list[str]) -> None:
                entered.set()
                await release.wait()
                await super().unsubscribe(pvs)

        buffer = FakeBuffer()
        mgr = GraceManager(SlowIngest(subscribed={"PV:A"}), buffer, grace_config)

        await mgr.on_pv_deactivated(["PV:A"])
        await asyncio.wait_for(entered.wait(), timeout=5.0)  # timer elapsed, teardown in flight

        await mgr.on_pv_activated(["PV:A"])
        release.set()
        await asyncio.sleep(0.01)

        assert buffer.dropped == []
