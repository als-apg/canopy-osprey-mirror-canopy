"""Hare health endpoints."""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from canopy.main import app
from tests.conftest import READ_HEADERS
from canopy.hare.api.v1 import health as health_module
from canopy.core.security import require_read_access
from canopy.hare.dependencies import get_pv_broker


def test_watchdog_returns_heartbeat():
    with TestClient(app) as client:
        resp = client.get("/hare/v1/health/watchdog")
    assert resp.status_code == 200
    body = resp.json()
    assert body["module"] == "hare"
    assert "version" in body
    assert body["uptimeS"] >= 0
    assert body["nowTsNs"] > 0


def test_summary_reports_backend():
    with TestClient(app) as client:
        resp = client.get("/hare/v1/health/summary", headers=READ_HEADERS)
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["backend"] == "simulation"
    assert body["connectorOk"] is True
    assert body["warmMonitors"] == 0
    assert body["connected"] == 0
    # The built-in simulation connector exposes no identity: all three null.
    assert body["label"] is None
    assert body["notice"] is None
    assert body["binding"] is None


def test_summary_requires_a_key():
    with TestClient(app) as client:
        resp = client.get("/hare/v1/health/summary")
    assert resp.status_code == 401


class _Rollup:
    """A broker standing in by the two methods the summary reads."""

    def __init__(self, active_monitors: int, connected: int) -> None:
        self.active_monitors, self.connected = active_monitors, connected

    def status(self) -> dict:
        return {
            "running": True,
            "backend": "fake",
            "clients": 1,
            "active_monitors": self.active_monitors,
            "warm_monitors": 0,
            "connected": self.connected,
            "active_pvs": self.active_monitors,
            "subscriptions": self.active_monitors,
        }

    def heartbeat_frame(self) -> dict:
        return {
            "type": "heartbeat",
            "timestamp": 0.0,
            "backend": "fake",
            "label": None,
            "notice": None,
            "binding": None,
        }


@pytest.mark.parametrize(
    ("monitored", "connected", "status"),
    [(0, 0, "ok"), (53, 53, "ok"), (53, 1, "ok"), (53, 0, "degraded")],
)
def test_summary_is_degraded_only_when_monitors_exist_and_none_is_connected(monitored, connected, status):
    app = FastAPI()
    app.include_router(health_module.router)
    app.dependency_overrides[get_pv_broker] = lambda: _Rollup(monitored, connected)
    app.dependency_overrides[require_read_access] = lambda: None
    with TestClient(app) as client:
        body = client.get("/health/summary").json()
    assert (body["status"], body["activeMonitors"], body["connected"]) == (status, monitored, connected)
    assert body["connectorOk"] is True
