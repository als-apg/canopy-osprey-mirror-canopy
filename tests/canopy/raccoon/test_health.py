"""Raccoon `/health/watchdog` smoke test."""

from fastapi.testclient import TestClient

from canopy.main import app


def test_watchdog_returns_heartbeat() -> None:
    with TestClient(app) as client:
        response = client.get("/raccoon/v1/health/watchdog")

    assert response.status_code == 200
    body = response.json()
    assert body["module"] == "raccoon"
    assert "version" in body
    assert "uptimeS" in body
    assert "nowTsNs" in body
    assert isinstance(body["nowTsNs"], int)
    assert body["uptimeS"] >= 0
