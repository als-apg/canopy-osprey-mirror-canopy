"""Integration tests for `GET /mole/v1/data/{pv}` (raw mode)."""

from tests.conftest import READ_HEADERS
from tests.canopy.mole.adapters._fakes import FakeArchiverAdapter


def test_data_endpoint_returns_samples(mole_client):
    response = mole_client.get(
        "/mole/v1/data/PV:TEST",
        params={"from": "2026-01-01T00:00:00Z", "to": "2026-01-01T00:00:10Z"},
        headers=READ_HEADERS,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["mode"] == "raw"
    assert body["pv"] == "PV:TEST"
    assert body["archiver"] == "primary"
    assert body["decimated"] is False
    assert isinstance(body["samples"], list)
    assert all(s["source"] == "archive" for s in body["samples"])
    # camelCase + the explicit `from`/`to` aliases on the wire
    assert "from" in body
    assert "to" in body


def test_data_endpoint_default_archiver_when_omitted(mole_client):
    r = mole_client.get(
        "/mole/v1/data/PV:TEST",
        params={"from": "0", "to": "10000000000"},
        headers=READ_HEADERS,
    )
    assert r.status_code == 200
    assert r.json()["archiver"] == "primary"


def test_data_endpoint_archiver_override(mole_client, test_service):
    """An explicit archiver routes to the named instance."""
    secondary = FakeArchiverAdapter(name="secondary")
    test_service._registry._adapters["secondary"] = secondary
    r = mole_client.get(
        "/mole/v1/data/PV:TEST",
        params={"from": "0", "to": "10000000000", "archiver": "secondary"},
        headers=READ_HEADERS,
    )
    assert r.status_code == 200
    assert r.json()["archiver"] == "secondary"


def test_data_endpoint_unknown_archiver_404(mole_client):
    r = mole_client.get(
        "/mole/v1/data/PV:TEST",
        params={"from": "0", "to": "10000000000", "archiver": "missing"},
        headers=READ_HEADERS,
    )
    assert r.status_code == 404
    body = r.json()
    assert body["code"] == "archiver_not_configured"
    assert "missing" in body["detail"]


def test_data_endpoint_unknown_pv_404(mole_client):
    r = mole_client.get(
        "/mole/v1/data/PV:UNKNOWN",  # not in fake's pvs dict
        params={"from": "0", "to": "10000000000"},
        headers=READ_HEADERS,
    )
    assert r.status_code == 404
    body = r.json()
    assert body["code"] == "pv_not_found_in_archiver"


def test_data_endpoint_iso_with_z_parses(mole_client):
    r = mole_client.get(
        "/mole/v1/data/PV:TEST",
        params={"from": "2026-05-15T10:30:00Z"},
        headers=READ_HEADERS,
    )
    assert r.status_code == 200


def test_data_endpoint_invalid_time_400(mole_client):
    r = mole_client.get(
        "/mole/v1/data/PV:TEST",
        params={"from": "not-a-time"},
        headers=READ_HEADERS,
    )
    assert r.status_code == 400
    assert r.json()["code"] == "bad_request"


def test_data_endpoint_from_after_to_400(mole_client):
    r = mole_client.get(
        "/mole/v1/data/PV:TEST",
        params={"from": "10000000000", "to": "5000000000"},
        headers=READ_HEADERS,
    )
    assert r.status_code == 400
    assert r.json()["code"] == "bad_request"


def test_data_endpoint_binned_mode_not_yet_400(mole_client):
    """Binned mode is rejected until Commit 6."""
    r = mole_client.get(
        "/mole/v1/data/PV:TEST",
        params={"from": "0", "to": "10000000000", "binning": "auto", "maxPoints": 100},
        headers=READ_HEADERS,
    )
    assert r.status_code == 400


def test_data_endpoint_no_auth_401(mole_client):
    """Missing X-API-Key is rejected with the slug envelope."""
    r = mole_client.get(
        "/mole/v1/data/PV:TEST",
        params={"from": "0"},
        # no auth header
    )
    assert r.status_code == 401
    assert r.json()["code"] == "invalid_api_key"


def test_data_endpoint_max_points_no_decimation_with_fake(mole_client):
    """The Fake honors max_points, so decimated stays False."""
    r = mole_client.get(
        "/mole/v1/data/PV:TEST",
        params={"from": "0", "to": "1000000000", "maxPoints": 3},
        headers=READ_HEADERS,
    )
    assert r.status_code == 200
    body = r.json()
    assert body["decimated"] is False
    assert len(body["samples"]) == 3
