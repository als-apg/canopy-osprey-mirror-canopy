"""Hare health endpoints."""

from fastapi.testclient import TestClient

from canopy.main import app
from tests.conftest import READ_HEADERS


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
    # The built-in simulation connector exposes no identity: all three null.
    assert body["label"] is None
    assert body["notice"] is None
    assert body["binding"] is None


def test_summary_requires_a_key():
    with TestClient(app) as client:
        resp = client.get("/hare/v1/health/summary")
    assert resp.status_code == 401
