"""Integration tests for `GET /mole/v1/metadata/{pv}`."""

from tests.conftest import READ_HEADERS


def test_metadata_endpoint_returns_known_fields(mole_client):
    r = mole_client.get("/mole/v1/metadata/PV:TEST", headers=READ_HEADERS)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["pv"] == "PV:TEST"
    assert body["archiver"] == "primary"
    assert body["units"] == "V"
    assert body["precision"] == 3
    # Other fields stay null since Appliance adapter doesn't populate them.
    assert body["description"] is None
    assert body["alarmLow"] is None


def test_metadata_endpoint_unknown_pv_404(mole_client):
    r = mole_client.get("/mole/v1/metadata/PV:UNKNOWN", headers=READ_HEADERS)
    assert r.status_code == 404
    assert r.json()["code"] == "pv_not_found_in_archiver"


def test_metadata_endpoint_unknown_archiver_404(mole_client):
    r = mole_client.get(
        "/mole/v1/metadata/PV:TEST",
        params={"archiver": "missing"},
        headers=READ_HEADERS,
    )
    assert r.status_code == 404
    assert r.json()["code"] == "archiver_not_configured"


def test_metadata_endpoint_no_auth_401(mole_client):
    r = mole_client.get("/mole/v1/metadata/PV:TEST")
    assert r.status_code == 401
