"""Beaver health endpoints. Uses the real lifespan and real catalog."""

from fastapi.testclient import TestClient

from canopy.main import app
from tests.conftest import READ_HEADERS


def test_watchdog_returns_heartbeat():
    with TestClient(app) as client:
        resp = client.get("/beaver/v1/health/watchdog")
    assert resp.status_code == 200
    body = resp.json()
    assert body["module"] == "beaver"
    assert "version" in body
    assert body["uptimeS"] >= 0
    assert body["nowTsNs"] > 0


def test_summary_reports_widget_count():
    with TestClient(app) as client:
        resp = client.get("/beaver/v1/health/summary", headers=READ_HEADERS)
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["widgetCount"] >= 1
    assert body["categoryCount"] >= 1
    assert body["registryVersion"]
