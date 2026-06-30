"""Converter health endpoints."""

from fastapi.testclient import TestClient

from canopy.main import app


def test_watchdog_returns_heartbeat():
    with TestClient(app) as client:
        resp = client.get("/converter/v1/health/watchdog")
    assert resp.status_code == 200
    body = resp.json()
    assert body["module"] == "converter"
    assert "version" in body
    assert body["uptimeS"] >= 0
    assert body["nowTsNs"] > 0
