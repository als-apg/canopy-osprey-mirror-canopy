"""Formula Engine health endpoint tests."""

from fastapi.testclient import TestClient

from canopy.main import app


def test_watchdog_returns_heartbeat():
    with TestClient(app) as client:
        response = client.get("/formula-engine/v1/health/watchdog")

    assert response.status_code == 200
    body = response.json()
    assert body["module"] == "formula-engine"
    assert "version" in body
    assert body["uptimeS"] >= 0
    assert body["nowTsNs"] > 0


def test_health_tag_matches_the_other_formula_engine_groups():
    # module.capitalize() would give "Formula-engine / Health".
    with TestClient(app) as client:
        spec = client.get("/formula-engine/openapi.json").json()

    assert spec["paths"]["/v1/health/watchdog"]["get"]["tags"] == ["Formula Engine / Health"]
