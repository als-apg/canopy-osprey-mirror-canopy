"""Hare's module gauges read the broker at scrape time, mirroring /health/summary."""

import pytest
from prometheus_client import REGISTRY
from fastapi.testclient import TestClient
from prometheus_client.parser import text_string_to_metric_families

from canopy.hare import gateway
from canopy.main import app
from tests.conftest import READ_HEADERS
from canopy.core.metrics import render_latest


def _gauge(name: str) -> float:
    return REGISTRY.get_sample_value(name)


def test_gauges_agree_with_the_health_summary_while_a_client_is_subscribed():
    with TestClient(app) as client:
        with client.websocket_connect("/hare/v1/ws/pvs?api_key=test-read") as ws:
            ws.send_json({"type": "subscribe", "pvNames": ["SIM:METRICS:A", "SIM:METRICS:B"]})
            for _ in range(5):
                if ws.receive_json()["type"] == "initial":
                    break
            else:
                pytest.fail("no initial frame among the first 5 messages")

            summary = client.get("/hare/v1/health/summary", headers=READ_HEADERS).json()
            assert summary["subscriptions"] >= 2

            assert _gauge("canopy_hare_clients") == summary["clients"]
            assert _gauge("canopy_hare_active_monitors") == summary["activeMonitors"]
            assert _gauge("canopy_hare_subscriptions") == summary["subscriptions"]


def test_gauges_read_zero_once_the_lifespan_has_unbound_the_broker():
    with TestClient(app):
        pass
    assert _gauge("canopy_hare_clients") == 0
    assert _gauge("canopy_hare_active_monitors") == 0
    assert _gauge("canopy_hare_subscriptions") == 0


def test_a_failing_broker_status_drops_hare_series_but_not_the_scrape(monkeypatch, caplog):
    """One module's fault must not cost Prometheus every series in the process."""

    class BrokenBroker:
        def status(self):
            raise RuntimeError("half torn down")

    monkeypatch.setattr(gateway, "get_broker", lambda: BrokenBroker())
    payload, _ = render_latest()
    names = {family.name for family in text_string_to_metric_families(payload.decode())}
    assert "canopy_http_requests" in names
    assert "canopy_hare_clients" not in names
    assert "Hare metrics skipped this scrape" in caplog.text
