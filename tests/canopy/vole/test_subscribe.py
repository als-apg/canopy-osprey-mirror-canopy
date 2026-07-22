"""Tests for the Vole PV subscribe endpoint and GraceManager lifecycle."""

import asyncio

import pytest
from fastapi.testclient import TestClient

from tests.conftest import READ_HEADERS
from canopy.vole.config import VoleConfig
from canopy.vole.services.grace_manager import GraceManager


class FakeIngest:
    """Minimal stand-in for HareIngestService for GraceManager tests."""

    def __init__(self, subscribed: set[str] | None = None) -> None:
        self._subscribed_pvs: set[str] = subscribed or set()
        self.unsubscribed: list[list[str]] = []

    async def subscribe(self, pvs: list[str]) -> None:
        self._subscribed_pvs.update(pvs)

    async def unsubscribe(self, pvs: list[str]) -> None:
        self._subscribed_pvs -= set(pvs)
        self.unsubscribed.append(pvs)


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
        await asyncio.sleep(0.05)

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
