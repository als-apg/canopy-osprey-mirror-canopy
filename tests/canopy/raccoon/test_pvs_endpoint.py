"""Integration tests for `/raccoon/v1/pvs*`."""

from tests.conftest import READ_HEADERS


def test_list_pvs_returns_names(raccoon_client, fake_adapter):
    fake_adapter.pvs = {"BPMS:1", "BPMS:2", "XCOR:1"}
    r = raccoon_client.get(
        "/raccoon/v1/pvs",
        params={"pattern": "BPMS"},
        headers=READ_HEADERS,
    )
    assert r.status_code == 200, r.text
    assert r.json() == {"names": ["BPMS:1", "BPMS:2"]}


def test_list_pvs_camelcase_query_params(raccoon_client, fake_adapter):
    fake_adapter.pvs = {"BPMS:1"}
    r = raccoon_client.get(
        "/raccoon/v1/pvs",
        params={"pattern": "BPMS", "sortBy": "name", "elementType": "QUAD"},
        headers=READ_HEADERS,
    )
    assert r.status_code == 200, r.text


def test_list_pvs_no_auth_401(raccoon_client):
    r = raccoon_client.get("/raccoon/v1/pvs", params={"pattern": "X"})
    assert r.status_code == 401


def test_exists_endpoint_true(raccoon_client, fake_adapter):
    fake_adapter.pvs = {"FOO:1"}
    r = raccoon_client.get("/raccoon/v1/pvs/FOO:1/exists", headers=READ_HEADERS)
    assert r.status_code == 200
    assert r.json() == {"pv": "FOO:1", "exists": True}


def test_exists_endpoint_false(raccoon_client):
    r = raccoon_client.get("/raccoon/v1/pvs/MISSING:1/exists", headers=READ_HEADERS)
    assert r.status_code == 200
    assert r.json() == {"pv": "MISSING:1", "exists": False}


def test_exists_endpoint_pattern_metachar_rejected(raccoon_client):
    r = raccoon_client.get("/raccoon/v1/pvs/FOO%/exists", headers=READ_HEADERS)
    assert r.status_code == 400
    assert r.json()["code"] == "invalid_query"


def test_validate_endpoint_partitions_and_preserves_order(raccoon_client, fake_adapter):
    fake_adapter.pvs = {"A:1", "C:1"}
    r = raccoon_client.post(
        "/raccoon/v1/pvs/validate",
        json={"pvs": ["A:1", "B:1", "C:1"]},
        headers=READ_HEADERS,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body == {"known": ["A:1", "C:1"], "unknown": ["B:1"]}


def test_validate_endpoint_handles_large_input(raccoon_client, fake_adapter):
    fake_adapter.pvs = {f"PV:{i}" for i in range(0, 5000, 2)}
    payload = [f"PV:{i}" for i in range(5000)]
    r = raccoon_client.post(
        "/raccoon/v1/pvs/validate",
        json={"pvs": payload},
        headers=READ_HEADERS,
    )
    assert r.status_code == 200
    body = r.json()
    assert len(body["known"]) == 2500
    assert len(body["unknown"]) == 2500


def test_validate_endpoint_pattern_metachar_rejected(raccoon_client):
    r = raccoon_client.post(
        "/raccoon/v1/pvs/validate",
        json={"pvs": ["A:1", "B|C"]},
        headers=READ_HEADERS,
    )
    assert r.status_code == 400
    assert r.json()["code"] == "invalid_query"


def test_validate_endpoint_no_auth_401(raccoon_client):
    r = raccoon_client.post("/raccoon/v1/pvs/validate", json={"pvs": []})
    assert r.status_code == 401
