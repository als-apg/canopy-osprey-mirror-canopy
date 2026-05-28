"""Fox health endpoint tests."""

from fastapi.testclient import TestClient

from canopy.main import app


def test_watchdog_returns_heartbeat():
    with TestClient(app) as client:
        response = client.get("/fox/v1/health/watchdog")

    assert response.status_code == 200
    body = response.json()
    assert body["module"] == "fox"
    assert "version" in body
    assert body["uptimeS"] >= 0
    assert body["nowTsNs"] > 0
