"""Tests for Vole health endpoints."""

from tests.conftest import READ_HEADERS, ADMIN_HEADERS, WRITE_HEADERS


class TestHealthEndpoints:
    def test_watchdog(self, vole_client):
        resp = vole_client.get("/vole/v1/health/watchdog")
        assert resp.status_code == 200
        data = resp.json()
        assert data["module"] == "vole"
        assert "uptimeS" in data
        assert "ringCount" in data

    def test_summary_requires_auth(self, vole_client):
        resp = vole_client.get("/vole/v1/health/summary")
        assert resp.status_code == 401

    def test_summary_with_auth(self, vole_client):
        resp = vole_client.get("/vole/v1/health/summary", headers=READ_HEADERS)
        assert resp.status_code == 200
        data = resp.json()
        assert "ringCount" in data
        assert "totalPoints" in data
        assert "totalBytes" in data
        assert "pressure" in data


class TestDebugEndpoints:
    def test_buffers_requires_auth(self, vole_client):
        resp = vole_client.get("/vole/v1/_debug/buffers")
        assert resp.status_code == 401

    def test_buffers_with_read_access(self, vole_client):
        resp = vole_client.get("/vole/v1/_debug/buffers", headers=READ_HEADERS)
        assert resp.status_code == 200

    def test_evict_pv(self, vole_client):
        resp = vole_client.post("/vole/v1/_debug/evict/PV:TEST", headers=ADMIN_HEADERS)
        assert resp.status_code == 200
        assert resp.json()["pv"] == "PV:TEST"

    def test_evict_pv_requires_admin_not_write(self, vole_client):
        # Dropping a ring buffer is an operational action, not a data write —
        # a write-scoped key must not be able to do it.
        resp = vole_client.post("/vole/v1/_debug/evict/PV:TEST", headers=WRITE_HEADERS)
        assert resp.status_code == 403
        assert resp.json()["code"] == "insufficient_admin_access"
