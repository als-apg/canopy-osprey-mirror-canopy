"""Analytics ingestion endpoint (`POST /analytics/v1/events`) on the composed app."""

import logging

import pytest
from fastapi.testclient import TestClient

from canopy.main import app
from tests.conftest import READ_HEADERS
from canopy.analytics import gateway
from canopy.analytics.app import app as analytics_app
from canopy.analytics.config import AnalyticsConfig
from canopy.analytics.dependencies import get_config, get_analytics_service
from canopy.analytics.services.analytics_service import AnalyticsService

EVENT = {
    "event": "screen_opened",
    "sessionId": "s1",
    "route": "/demo",
    "path": "/demo?x=1",
    "clientTs": "2026-07-20T12:00:00Z",
    "properties": {"screen": "demo"},
}


def _analytics_records(caplog):
    return [r for r in caplog.records if r.name == "canopy.analytics"]


def test_anonymous_event_accepted_and_recorded(caplog):
    caplog.set_level(logging.INFO, logger="canopy.analytics")
    with TestClient(app) as client:
        resp = client.post("/analytics/v1/events", json=EVENT)
    assert resp.status_code == 204
    (record,) = _analytics_records(caplog)
    assert record.event == "screen_opened"
    assert record.session_id == "s1"
    assert record.route == "/demo"
    assert record.client_ts == "2026-07-20T12:00:00+00:00"
    assert record.properties == {"screen": "demo"}
    assert record.has_api_key is False
    assert record.api_key_scopes is None
    assert record.user_agent  # TestClient sends a User-Agent header


def test_valid_key_enriches_but_token_not_logged(caplog):
    caplog.set_level(logging.INFO, logger="canopy.analytics")
    with TestClient(app) as client:
        resp = client.post("/analytics/v1/events", json=EVENT, headers=READ_HEADERS)
    assert resp.status_code == 204
    (record,) = _analytics_records(caplog)
    assert record.has_api_key is True
    assert record.api_key_scopes == "read"
    assert record.api_key_name == "env:1"
    # The credential itself must never appear in the record.
    assert "test-read" not in str(record.__dict__)


def test_invalid_key_is_ignored_not_rejected(caplog):
    caplog.set_level(logging.INFO, logger="canopy.analytics")
    with TestClient(app) as client:
        resp = client.post("/analytics/v1/events", json=EVENT, headers={"X-API-Key": "bogus"})
    assert resp.status_code == 204
    (record,) = _analytics_records(caplog)
    assert record.has_api_key is False


def test_disabled_config_accepts_but_drops(caplog):
    caplog.set_level(logging.INFO, logger="canopy.analytics")
    analytics_app.dependency_overrides[get_analytics_service] = lambda: AnalyticsService(AnalyticsConfig(enabled=False))
    try:
        with TestClient(app) as client:
            resp = client.post("/analytics/v1/events", json=EVENT)
    finally:
        analytics_app.dependency_overrides.pop(get_analytics_service)
    assert resp.status_code == 204
    assert _analytics_records(caplog) == []


def test_missing_event_field_is_422():
    with TestClient(app) as client:
        resp = client.post("/analytics/v1/events", json={"sessionId": "s1"})
    assert resp.status_code == 422


def test_minimal_event_needs_only_name(caplog):
    caplog.set_level(logging.INFO, logger="canopy.analytics")
    with TestClient(app) as client:
        resp = client.post("/analytics/v1/events", json={"event": "ping"})
    assert resp.status_code == 204
    (record,) = _analytics_records(caplog)
    assert record.event == "ping"
    assert record.properties == {}


def test_watchdog_returns_heartbeat():
    with TestClient(app) as client:
        resp = client.get("/analytics/v1/health/watchdog")
    assert resp.status_code == 200
    body = resp.json()
    assert body["module"] == "analytics"
    assert body["uptimeS"] >= 0


def test_oversized_event_is_rejected_before_recording(caplog):
    """The size guard fires before anything reaches the log."""
    caplog.set_level(logging.INFO, logger="canopy.analytics")
    analytics_app.dependency_overrides[get_config] = lambda: AnalyticsConfig(max_event_bytes=64)
    try:
        with TestClient(app) as client:
            resp = client.post(
                "/analytics/v1/events",
                json={"event": "frontend_error", "properties": {"stack": "x" * 200}},
            )
    finally:
        analytics_app.dependency_overrides.pop(get_config, None)
    assert resp.status_code == 413
    assert resp.json()["code"] == "analytics_event_too_large"
    assert _analytics_records(caplog) == []


def test_service_bound_for_app_lifetime_only():
    with TestClient(app):
        assert isinstance(gateway.get_service(), AnalyticsService)
    with pytest.raises(RuntimeError):
        gateway.get_service()
