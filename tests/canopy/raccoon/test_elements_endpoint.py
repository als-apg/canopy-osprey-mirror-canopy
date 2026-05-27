"""Integration tests for `/raccoon/v1/elements`."""

from tests.conftest import READ_HEADERS


def test_list_elements_returns_names(raccoon_client, fake_adapter):
    fake_adapter.elements = {"EL:A", "EL:B"}
    r = raccoon_client.get(
        "/raccoon/v1/elements",
        params={"pattern": "EL"},
        headers=READ_HEADERS,
    )
    assert r.status_code == 200, r.text
    assert r.json() == {"names": ["EL:A", "EL:B"]}


def test_list_elements_no_auth_401(raccoon_client):
    r = raccoon_client.get("/raccoon/v1/elements", params={"pattern": "X"})
    assert r.status_code == 401
