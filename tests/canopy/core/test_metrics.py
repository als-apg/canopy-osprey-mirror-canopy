"""canopy.core.metrics — HTTP middleware labels + /metrics exposition."""

import asyncio

import pytest
from prometheus_client import REGISTRY
from fastapi.testclient import TestClient
from prometheus_client.parser import text_string_to_metric_families

from canopy.main import app
from tests.conftest import READ_HEADERS
from canopy.core.metrics import HTTPMetricsMiddleware


def _requests_total(**labels) -> float:
    return REGISTRY.get_sample_value("canopy_http_requests_total", labels) or 0.0


def test_mounted_route_is_labelled_with_its_module_and_template():
    labels = dict(method="GET", module="hare", route="/v1/health/watchdog", status="200")
    before = _requests_total(**labels)
    with TestClient(app) as client:
        assert client.get("/hare/v1/health/watchdog").status_code == 200
    assert _requests_total(**labels) == before + 1


def test_parent_app_route_is_labelled_core():
    labels = dict(method="GET", module="core", route="/health", status="200")
    before = _requests_total(**labels)
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
    assert _requests_total(**labels) == before + 1


def test_path_params_never_become_labels():
    labels = dict(method="GET", module="hare", route="/v1/pvs/{pv}", status="200")
    before = _requests_total(**labels)
    with TestClient(app) as client:
        # Simulation EPICS backend: any PV name reads fine.
        assert client.get("/hare/v1/pvs/METRICS:TEST:PV", headers=READ_HEADERS).status_code == 200
        exposition = client.get("/metrics").text
    assert _requests_total(**labels) == before + 1
    assert "METRICS:TEST:PV" not in exposition


def test_unmatched_route_inside_a_module_keeps_the_module_label():
    """A 404 under /hare/ still went through the Hare app, so the module label
    is known and bounded; only the route collapses."""
    labels = dict(method="GET", module="hare", route="unmatched", status="404")
    before = _requests_total(**labels)
    with TestClient(app) as client:
        assert client.get("/hare/v1/no-such-route-xyz").status_code == 404
        exposition = client.get("/metrics").text
    assert _requests_total(**labels) == before + 1
    assert "no-such-route-xyz" not in exposition


def test_unmatched_path_outside_every_mount_is_labelled_core():
    """Bot scans must not mint series: no mount matched, so the parent app is
    the innermost app and nothing from the path reaches a label."""
    labels = dict(method="GET", module="core", route="unmatched", status="404")
    before = _requests_total(**labels)
    with TestClient(app) as client:
        assert client.get("/wp-admin/setup.php").status_code == 404
        assert client.get("/.env").status_code == 404
        exposition = client.get("/metrics").text
    assert _requests_total(**labels) == before + 2
    assert "wp-admin" not in exposition


def test_unknown_http_methods_collapse_into_other():
    """Starlette still routes an unknown method token (405 on a matched path),
    so the method label must be bounded too or a scanner could mint a series
    per spelling."""
    labels = dict(method="other", module="core", route="/health", status="405")
    before = _requests_total(**labels)
    with TestClient(app) as client:
        assert client.request("FOOBAR", "/health").status_code == 405
        assert client.request("BREW", "/health").status_code == 405
        exposition = client.get("/metrics").text
    assert _requests_total(**labels) == before + 2
    assert "FOOBAR" not in exposition
    assert "BREW" not in exposition


def test_cors_preflight_not_counted():
    """CORS answers preflights before routing; the metrics middleware sits
    inside CORS, so they never reach it."""
    labels = dict(method="OPTIONS", module="core", route="unmatched", status="200")
    before = _requests_total(**labels)
    with TestClient(app) as client:
        resp = client.options(
            "/hare/v1/pvs/METRICS:TEST:PV",
            headers={
                "Origin": "http://localhost:5173",
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": "x-api-key",
            },
        )
    assert resp.status_code == 200
    assert "access-control-allow-origin" in resp.headers
    assert _requests_total(**labels) == before


async def _never_called(*_):
    raise AssertionError("the fake apps below neither read a body nor send a response")


def _bare_scope() -> dict:
    """Straight into the middleware: the tagged parent app, no matched route."""
    return {"type": "http", "method": "GET", "app": app}


async def test_unhandled_exception_is_counted_as_500():
    """ServerErrorMiddleware, outside this one, turns the exception into a 500
    response, so that is what the request was."""

    async def failing_app(scope, receive, send):
        raise RuntimeError("boom")

    labels = dict(method="GET", module="core", route="unmatched", status="500")
    before = _requests_total(**labels)
    with pytest.raises(RuntimeError):
        await HTTPMetricsMiddleware(failing_app)(_bare_scope(), _never_called, _never_called)
    assert _requests_total(**labels) == before + 1


async def test_cancelled_before_any_response_is_not_counted():
    """A handler cancelled before it sent anything (uvicorn's graceful-shutdown
    timeout) produced no response, so there is no status to count, and the
    cancellation propagates untouched."""

    async def cancelled_app(scope, receive, send):
        raise asyncio.CancelledError()

    labels = dict(method="GET", module="core", route="unmatched", status="500")
    before = _requests_total(**labels)
    with pytest.raises(asyncio.CancelledError):
        await HTTPMetricsMiddleware(cancelled_app)(_bare_scope(), _never_called, _never_called)
    assert _requests_total(**labels) == before


def test_metrics_endpoint_is_unauthenticated_and_parseable():
    with TestClient(app) as client:
        resp = client.get("/metrics")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/plain")
    names = {family.name for family in text_string_to_metric_families(resp.text)}
    assert {
        "canopy_http_requests",
        "canopy_http_request_duration_seconds",
        "canopy_hare_clients",
        "canopy_hare_active_monitors",
        "canopy_hare_subscriptions",
    } <= names
