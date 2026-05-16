"""Integration tests for `GET /mole/v1/search`."""

from tests.conftest import READ_HEADERS


def test_search_endpoint_returns_matching_names(mole_client):
    r = mole_client.get(
        "/mole/v1/search",
        params={"pattern": "PV:.*"},
        headers=READ_HEADERS,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["archiver"] == "primary"
    assert body["pattern"] == "PV:.*"
    assert "PV:TEST" in body["pvs"]


def test_search_endpoint_respects_limit(mole_client, test_service):
    fake = test_service.registry.get(None)
    fake.pvs = {f"PV:T{i}": {"units": "V"} for i in range(20)}
    r = mole_client.get(
        "/mole/v1/search",
        params={"pattern": "PV:.*", "limit": 5},
        headers=READ_HEADERS,
    )
    assert r.status_code == 200
    body = r.json()
    assert len(body["pvs"]) == 5


def test_search_endpoint_no_match_returns_empty(mole_client):
    r = mole_client.get(
        "/mole/v1/search",
        params={"pattern": "DOESNOTMATCH"},
        headers=READ_HEADERS,
    )
    assert r.status_code == 200
    body = r.json()
    assert body["pvs"] == []


def test_search_endpoint_unknown_archiver_404(mole_client):
    r = mole_client.get(
        "/mole/v1/search",
        params={"pattern": ".*", "archiver": "missing"},
        headers=READ_HEADERS,
    )
    assert r.status_code == 404
    assert r.json()["code"] == "archiver_not_configured"


def test_search_endpoint_no_auth_401(mole_client):
    r = mole_client.get("/mole/v1/search", params={"pattern": ".*"})
    assert r.status_code == 401
