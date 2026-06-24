"""Hare REST PV endpoint."""

from fastapi.testclient import TestClient

from canopy.main import app
from tests.conftest import READ_HEADERS, WRITE_HEADERS


def test_get_pv_current_value():
    with TestClient(app) as client:
        resp = client.get("/hare/v1/pvs/SIM:REST", headers=READ_HEADERS)
    assert resp.status_code == 200
    body = resp.json()
    assert body["pv"] == "SIM:REST"
    assert body["connected"] is True
    assert isinstance(body["value"], int | float)


def test_get_pv_requires_auth():
    with TestClient(app) as client:
        resp = client.get("/hare/v1/pvs/SIM:REST")
    assert resp.status_code == 401


def test_describe_pv_returns_metadata():
    with TestClient(app) as client:
        resp = client.get("/hare/v1/pvs/SIM:STATE:ENUM/describe", headers=READ_HEADERS)
    assert resp.status_code == 200
    body = resp.json()
    assert body["pv"] == "SIM:STATE:ENUM"
    assert body["valueKind"] == "enum"
    assert body["enumChoices"] and len(body["enumChoices"]) >= 2


def test_describe_route_not_shadowed_by_value_route():
    # The greedy /{pv:path} catch-all must not swallow `.../describe`.
    with TestClient(app) as client:
        value = client.get("/hare/v1/pvs/SIM:REST", headers=READ_HEADERS)
        describe = client.get("/hare/v1/pvs/SIM:REST/describe", headers=READ_HEADERS)
    assert value.status_code == 200 and "value" in value.json()
    assert describe.status_code == 200 and describe.json()["valueKind"] == "scalar"


def test_put_pv_writes_value():
    with TestClient(app) as client:
        resp = client.put("/hare/v1/pvs/SIM:TEMP", json={"value": 42.0}, headers=WRITE_HEADERS)
    assert resp.status_code == 200
    assert resp.json() == {"pv": "SIM:TEMP"}


def test_put_pv_round_trips_through_get():
    # The simulation backend pins a written value, so a later read reflects it.
    with TestClient(app) as client:
        put = client.put("/hare/v1/pvs/SIM:PUT:RT", json={"value": 12.5}, headers=WRITE_HEADERS)
        get = client.get("/hare/v1/pvs/SIM:PUT:RT", headers=READ_HEADERS)
    assert put.status_code == 200
    assert get.status_code == 200
    assert get.json()["value"] == 12.5


def test_put_pv_requires_write_access():
    # A read-only key may read but not write.
    with TestClient(app) as client:
        resp = client.put("/hare/v1/pvs/SIM:TEMP", json={"value": 1.0}, headers=READ_HEADERS)
    assert resp.status_code == 403


def test_put_pv_requires_auth():
    with TestClient(app) as client:
        resp = client.put("/hare/v1/pvs/SIM:TEMP", json={"value": 1.0})
    assert resp.status_code == 401


def test_put_pv_rejects_null_value():
    # null is not a writable value -> 422 (client error), not a 502 from the IOC layer.
    with TestClient(app) as client:
        resp = client.put("/hare/v1/pvs/SIM:TEMP", json={"value": None}, headers=WRITE_HEADERS)
    assert resp.status_code == 422


def test_put_pv_rejects_nonpositive_timeout():
    with TestClient(app) as client:
        resp = client.put("/hare/v1/pvs/SIM:TEMP", json={"value": 1.0, "timeout": -5}, headers=WRITE_HEADERS)
    assert resp.status_code == 422
