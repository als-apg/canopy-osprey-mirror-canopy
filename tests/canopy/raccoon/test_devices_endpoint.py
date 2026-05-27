"""Integration tests for `/raccoon/v1/devices`."""

from tests.conftest import READ_HEADERS


def test_list_devices_returns_names(raccoon_client, fake_adapter):
    fake_adapter.devices = {"DEV:A", "DEV:B"}
    r = raccoon_client.get(
        "/raccoon/v1/devices",
        params={"pattern": "DEV"},
        headers=READ_HEADERS,
    )
    assert r.status_code == 200, r.text
    assert r.json() == {"names": ["DEV:A", "DEV:B"]}


def test_list_devices_no_auth_401(raccoon_client):
    r = raccoon_client.get("/raccoon/v1/devices", params={"pattern": "X"})
    assert r.status_code == 401
