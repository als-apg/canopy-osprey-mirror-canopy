"""Hare REST PV endpoint."""

from fastapi.testclient import TestClient

from canopy.main import app
from tests.conftest import READ_HEADERS


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
