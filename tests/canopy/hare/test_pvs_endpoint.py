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
