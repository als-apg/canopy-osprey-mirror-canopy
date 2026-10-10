"""Route tests for GET /jobs/v1/{id} and the health watchdog.

`gateway.get_status` is patched, so these need no running Redis.
"""

from fastapi.testclient import TestClient

from canopy.core import security
from canopy.jobs import gateway
from canopy.main import app as parent_app
from canopy.core.security import EnvKeyRegistry, bind_key_registry
from canopy.jobs.schemas.job import JobStatusDTO

READ_HEADERS = {"X-API-Key": "test-read"}


def test_health_watchdog_is_public_and_camelcase():
    with TestClient(parent_app) as client:
        r = client.get("/jobs/v1/health/watchdog")
    assert r.status_code == 200
    body = r.json()
    assert body["module"] == "jobs"
    assert "uptimeS" in body
    assert isinstance(body["nowTsNs"], int)


def test_get_job_requires_api_key():
    with TestClient(parent_app) as client:
        r = client.get("/jobs/v1/some-id")
    assert r.status_code == 401


def test_get_job_unknown_returns_404(monkeypatch):
    async def fake_status(job_id):
        return None

    monkeypatch.setattr(gateway, "get_status", fake_status)
    with TestClient(parent_app) as client:
        r = client.get("/jobs/v1/unknown", headers=READ_HEADERS)
    assert r.status_code == 404
    assert r.json()["code"] == "job_not_found"


def test_get_job_returns_status_camelcase(monkeypatch):
    dto = JobStatusDTO(id="j1", function="create_snapshot", status="running", progress=50, message="halfway")

    async def fake_status(job_id):
        return dto

    monkeypatch.setattr(gateway, "get_status", fake_status)
    with TestClient(parent_app) as client:
        r = client.get("/jobs/v1/j1", headers=READ_HEADERS)
    assert r.status_code == 200
    body = r.json()
    assert body["id"] == "j1"
    assert body["status"] == "running"
    assert body["progress"] == 50
    assert body["function"] == "create_snapshot"


def test_a_module_scoped_key_can_poll_the_job_it_enqueued(monkeypatch):
    # Squirrel/converter hand back statusUrl: /jobs/v1/{id}, a separate mount —
    # a key scoped to only their module must still be able to poll it.
    async def fake_status(job_id):
        return None

    monkeypatch.setattr(gateway, "get_status", fake_status)
    with TestClient(parent_app) as client:
        # The app's own lifespan rebinds the registry from CANOPY_API_KEYS on
        # startup, so the module-scoped fake has to go on after that runs.
        bind_key_registry(EnvKeyRegistry("squirrel-only:read@squirrel"))
        try:
            r = client.get("/jobs/v1/some-id", headers={"X-API-Key": "squirrel-only"})
        finally:
            security._registry = None
    assert r.status_code == 404, r.text
