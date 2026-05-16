"""Integration tests for `GET /mole/v1/archivers` and `GET /mole/v1/health/summary`."""

from canopy.mole.config import ArchiverInstanceConfig
from tests.conftest import READ_HEADERS
from tests.canopy.mole.adapters._fakes import FakeArchiverAdapter


def test_archivers_lists_configured(mole_client):
    r = mole_client.get("/mole/v1/archivers", headers=READ_HEADERS)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["default"] == "primary"
    assert len(body["archivers"]) == 1
    a = body["archivers"][0]
    assert a["name"] == "primary"
    assert a["typeName"] == "fake"
    assert a["healthy"] is True


def test_archivers_redacts_url_credentials(mole_client, test_service):
    """Embedded `user:pass@` is redacted in the response url."""
    test_service._config.archivers["primary"] = ArchiverInstanceConfig(
        type="fake",
        url="https://user:secret@host.example/path",
    )
    r = mole_client.get("/mole/v1/archivers", headers=READ_HEADERS)
    body = r.json()
    a = next(x for x in body["archivers"] if x["name"] == "primary")
    assert "secret" not in a["url"]
    assert "***" in a["url"]


def test_archivers_multi_instance(mole_client, test_service):
    secondary = FakeArchiverAdapter(name="secondary")
    test_service._registry._adapters["secondary"] = secondary
    test_service._config.archivers["secondary"] = ArchiverInstanceConfig(type="fake", url="http://x.example")
    r = mole_client.get("/mole/v1/archivers", headers=READ_HEADERS)
    body = r.json()
    assert {a["name"] for a in body["archivers"]} == {"primary", "secondary"}


def test_health_summary_ok_when_all_healthy(mole_client):
    r = mole_client.get("/mole/v1/health/summary", headers=READ_HEADERS)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "ok"
    assert len(body["archivers"]) == 1
    a = body["archivers"][0]
    assert a["name"] == "primary"
    assert a["healthy"] is True


def test_health_summary_degraded_with_mixed(mole_client, test_service):
    bad = FakeArchiverAdapter(name="bad", closed=True)
    test_service._registry._adapters["bad"] = bad
    r = mole_client.get("/mole/v1/health/summary", headers=READ_HEADERS)
    body = r.json()
    assert body["status"] == "degraded"


def test_health_summary_down_when_all_unhealthy(mole_client, test_service):
    primary = test_service._registry._adapters["primary"]
    primary.closed = True
    r = mole_client.get("/mole/v1/health/summary", headers=READ_HEADERS)
    body = r.json()
    assert body["status"] == "down"


def test_health_summary_ok_when_no_archivers_configured(mole_client, test_service):
    test_service._registry._adapters.clear()
    r = mole_client.get("/mole/v1/health/summary", headers=READ_HEADERS)
    body = r.json()
    assert body["status"] == "ok"
    assert body["archivers"] == []
