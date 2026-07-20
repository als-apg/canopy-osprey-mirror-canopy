"""Tests for the Vole PVs read endpoints."""

from typing import Literal

from starlette.testclient import TestClient

from tests.conftest import READ_HEADERS
from canopy.contracts.sample import Sample, AlarmSeverity
from canopy.vole.services.buffer_service import BufferService

_BASE = 1_000_000_000_000 - 50_000_000_000


def _sample(ts_ns: int, value: float = 1.0, source: Literal["live", "archive"] = "archive") -> Sample:
    return Sample(ts_ns=_BASE + ts_ns, value=value, severity=AlarmSeverity.NO_ALARM, source=source)


class TestSinglePVRead:
    async def test_happy_path(self, vole_client: TestClient, test_service: BufferService) -> None:
        samples = [_sample(100, 10.0), _sample(200, 20.0), _sample(300, 30.0)]
        await test_service.seed("PV:A", samples)

        resp = vole_client.get(
            "/vole/v1/pvs/PV:A",
            params={"from": str(_BASE), "to": str(_BASE + 400)},
            headers=READ_HEADERS,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["pv"] == "PV:A"
        assert data["count"] == 3
        assert len(data["samples"]) == 3
        assert data["samples"][0]["tsNs"] == _BASE + 100
        assert data["samples"][0]["value"] == 10.0

    async def test_pv_not_buffered_returns_404(self, vole_client: TestClient) -> None:
        resp = vole_client.get(
            "/vole/v1/pvs/PV:NOPE",
            params={"from": str(_BASE), "to": str(_BASE + 100)},
            headers=READ_HEADERS,
        )
        assert resp.status_code == 404
        assert resp.json()["code"] == "pv_not_buffered"

    async def test_empty_window_returns_200(self, vole_client: TestClient, test_service: BufferService) -> None:
        await test_service.seed("PV:A", [_sample(100, 1.0)])

        resp = vole_client.get(
            "/vole/v1/pvs/PV:A",
            params={"from": str(_BASE + 500), "to": str(_BASE + 600), "align": "none"},
            headers=READ_HEADERS,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["count"] == 0
        assert data["samples"] == []

    async def test_decimation_via_max_points(self, vole_client: TestClient, test_service: BufferService) -> None:
        samples = [_sample(i * 100, float(i)) for i in range(20)]
        await test_service.seed("PV:A", samples)

        resp = vole_client.get(
            "/vole/v1/pvs/PV:A",
            params={"from": str(_BASE), "to": str(_BASE + 2000), "maxPoints": "5"},
            headers=READ_HEADERS,
        )
        assert resp.status_code == 200
        assert resp.json()["count"] <= 5

    async def test_to_omitted_defaults_to_now(self, vole_client: TestClient, test_service: BufferService) -> None:
        samples = [_sample(100, 10.0), _sample(200, 20.0)]
        await test_service.seed("PV:A", samples)

        resp = vole_client.get(
            "/vole/v1/pvs/PV:A",
            params={"from": str(_BASE)},
            headers=READ_HEADERS,
        )
        assert resp.status_code == 200
        assert resp.json()["count"] == 2

    async def test_from_after_to_returns_400(self, vole_client: TestClient, test_service: BufferService) -> None:
        await test_service.seed("PV:A", [_sample(100)])

        resp = vole_client.get(
            "/vole/v1/pvs/PV:A",
            params={"from": str(_BASE + 500), "to": str(_BASE + 100)},
            headers=READ_HEADERS,
        )
        assert resp.status_code == 400

    async def test_align_last_pins_pre_window_sample(
        self, vole_client: TestClient, test_service: BufferService
    ) -> None:
        samples = [_sample(100, 1.0), _sample(300, 3.0)]
        await test_service.seed("PV:A", samples)

        resp = vole_client.get(
            "/vole/v1/pvs/PV:A",
            params={"from": str(_BASE + 200), "to": str(_BASE + 400), "align": "last"},
            headers=READ_HEADERS,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["samples"][0]["tsNs"] == _BASE + 200
        assert data["samples"][0]["value"] == 1.0


class TestMultiPVAlignedRead:
    async def test_happy_path(self, vole_client: TestClient, test_service: BufferService) -> None:
        await test_service.seed("PV:A", [_sample(100, 1.0), _sample(200, 2.0)])
        await test_service.seed("PV:B", [_sample(100, 10.0), _sample(200, 20.0)])

        resp = vole_client.get(
            "/vole/v1/pvs/",
            params={"pvs": "PV:A,PV:B", "from": str(_BASE), "to": str(_BASE + 300)},
            headers=READ_HEADERS,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["tsNs"]) > 0
        assert len(data["pvs"]) == 2
        assert data["pvs"][0]["pv"] == "PV:A"
        assert data["pvs"][1]["pv"] == "PV:B"
        assert len(data["pvs"][0]["values"]) == len(data["tsNs"])

    async def test_pv_not_buffered_returns_404(self, vole_client: TestClient, test_service: BufferService) -> None:
        await test_service.seed("PV:A", [_sample(100)])

        resp = vole_client.get(
            "/vole/v1/pvs/",
            params={"pvs": "PV:A,PV:MISSING", "from": str(_BASE), "to": str(_BASE + 200)},
            headers=READ_HEADERS,
        )
        assert resp.status_code == 404
        assert resp.json()["code"] == "pv_not_buffered"

    async def test_empty_pvs_returns_400(self, vole_client: TestClient) -> None:
        resp = vole_client.get(
            "/vole/v1/pvs/",
            params={"pvs": "", "from": str(_BASE), "to": str(_BASE + 100)},
            headers=READ_HEADERS,
        )
        assert resp.status_code == 400

    async def test_grid_index_without_grid_dt_returns_400(
        self, vole_client: TestClient, test_service: BufferService
    ) -> None:
        await test_service.seed("PV:A", [_sample(100)])

        resp = vole_client.get(
            "/vole/v1/pvs/",
            params={"pvs": "PV:A", "from": str(_BASE), "to": str(_BASE + 200), "index": "grid"},
            headers=READ_HEADERS,
        )
        assert resp.status_code == 400

    async def test_grid_index_with_grid_dt(self, vole_client: TestClient, test_service: BufferService) -> None:
        await test_service.seed("PV:A", [_sample(100, 1.0), _sample(200, 2.0), _sample(300, 3.0)])

        resp = vole_client.get(
            "/vole/v1/pvs/",
            params={
                "pvs": "PV:A",
                "from": str(_BASE + 100),
                "to": str(_BASE + 300),
                "index": "grid",
                "gridDtNs": "100",
            },
            headers=READ_HEADERS,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["tsNs"]) > 0
        assert len(data["pvs"][0]["values"]) == len(data["tsNs"])

    async def test_to_omitted_defaults_to_now(self, vole_client: TestClient, test_service: BufferService) -> None:
        await test_service.seed("PV:A", [_sample(100, 1.0)])
        await test_service.seed("PV:B", [_sample(100, 2.0)])

        resp = vole_client.get(
            "/vole/v1/pvs/",
            params={"pvs": "PV:A,PV:B", "from": str(_BASE)},
            headers=READ_HEADERS,
        )
        data = resp.json()

        assert resp.status_code == 200
        assert len(data["pvs"]) == 2
        assert len(data["pvs"][0]["values"]) == len(data["tsNs"])
