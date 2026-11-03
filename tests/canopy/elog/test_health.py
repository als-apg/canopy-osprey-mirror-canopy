"""`/elog/v1/health/*`: the shared watchdog and the per-app token probe rollup."""

import pytest

from tests.conftest import READ_HEADERS
from canopy.elog.app import app as elog_app
from canopy.elog.dependencies import get_elog_service
from tests.canopy.elog._fakes import FakeElogAdapter
from canopy.elog.adapters.base import AdapterHealth
from tests.canopy.elog.conftest import build_service

DOWN = AdapterHealth(healthy=False, latency_ms=3.0, last_error="elog-plus returned 401: Not authenticated")


@pytest.fixture
def summary(elog_client, monkeypatch):
    """Serve a service over ``adapters`` and return its health summary body."""

    def _summary(adapters: dict[str, FakeElogAdapter], *, enabled: bool = True) -> dict:
        service = build_service(adapters, enabled=enabled)
        monkeypatch.setitem(elog_app.dependency_overrides, get_elog_service, lambda: service)
        r = elog_client.get("/elog/v1/health/summary", headers=READ_HEADERS)
        assert r.status_code == 200, r.text
        return r.json()

    return _summary


def test_watchdog_needs_no_key(elog_client):
    r = elog_client.get("/elog/v1/health/watchdog")

    assert r.status_code == 200
    assert r.json()["module"] == "elog"


def test_summary_needs_read_access(elog_client):
    assert elog_client.get("/elog/v1/health/summary").status_code == 401


def test_all_apps_healthy_is_ok(summary):
    body = summary({"squirrel": FakeElogAdapter("squirrel"), "trace": FakeElogAdapter("trace")})

    assert body["status"] == "ok"
    assert body["provider"] == "fake"
    assert body["reason"] is None
    assert body["apps"] == [
        {"app": "squirrel", "healthy": True, "identity": "squirrel@app.elog", "latencyMs": 1.0, "lastError": None},
        {"app": "trace", "healthy": True, "identity": "trace@app.elog", "latencyMs": 1.0, "lastError": None},
    ]


def test_one_app_down_is_degraded(summary):
    trace = FakeElogAdapter("trace")
    trace.health_result = DOWN

    body = summary({"squirrel": FakeElogAdapter("squirrel"), "trace": trace})

    assert body["status"] == "degraded"
    assert body["apps"][1] == {
        "app": "trace",
        "healthy": False,
        "identity": None,
        "latencyMs": 3.0,
        "lastError": "elog-plus returned 401: Not authenticated",
    }


def test_every_app_down_is_down(summary):
    squirrel = FakeElogAdapter("squirrel")
    squirrel.health_result = DOWN

    body = summary({"squirrel": squirrel})

    assert body["status"] == "down"
    # Per-app failures explain themselves in lastError.
    assert body["reason"] is None


def test_no_provider_is_disabled_with_its_reason(summary):
    assert summary({}, enabled=False) == {
        "status": "disabled",
        "provider": None,
        "reason": "No e-log provider is configured (CANOPY_ELOG__PROVIDER is empty)",
        "apps": [],
    }


def test_provider_without_any_app_token_is_down_not_disabled(summary):
    # A pod deployed before its tokens exist: misconfigured, not switched off.
    assert summary({}) == {
        "status": "down",
        "provider": "fake",
        "reason": "No app has an e-log token configured (CANOPY_ELOG__APPS__<APP>__TOKEN)",
        "apps": [],
    }
