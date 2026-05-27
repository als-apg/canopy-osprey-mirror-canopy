"""`/raccoon/v1/health/summary` returns adapter health and cache stats."""

from tests.conftest import READ_HEADERS


def test_summary_includes_adapter_and_cache_stats(raccoon_client, fake_adapter):
    fake_adapter.pvs = {"A:1"}
    raccoon_client.get(
        "/raccoon/v1/pvs",
        params={"pattern": "A"},
        headers=READ_HEADERS,
    )

    r = raccoon_client.get("/raccoon/v1/health/summary", headers=READ_HEADERS)
    assert r.status_code == 200, r.text
    body = r.json()

    assert body["adapterName"] == "fake"
    assert body["adapterType"] == "fake"
    assert body["adapterHealth"]["healthy"] is True
    assert body["patternCache"]["size"] >= 1
    assert "nameCache" in body
    assert "inFlight" in body


def test_summary_no_auth_401(raccoon_client):
    r = raccoon_client.get("/raccoon/v1/health/summary")
    assert r.status_code == 401
