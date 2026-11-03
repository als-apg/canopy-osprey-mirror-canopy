"""Integration test: seed Vole then evaluate a Formula Engine formula."""

import pytest
from fastapi.testclient import TestClient

from canopy.main import app as parent_app
from tests.conftest import READ_HEADERS
from canopy.vole.app import app as vole_app
from canopy.core.clock import FakeClock
from canopy.vole.config import VoleConfig
from canopy.vole.adapter import VoleDataSource
from canopy.contracts.sample import Sample, AlarmSeverity
from canopy.vole.buffer.store import PVStore
from canopy.vole.dependencies import get_buffer_service
from canopy.formula_engine.app import app as formula_engine_app
from canopy.formula_engine.config import FormulaEngineConfig
from canopy.formula_engine.dependencies import get_formula_service
from canopy.vole.services.buffer_service import BufferService
from canopy.formula_engine.services.parser import Parser
from canopy.vole.services.eviction_service import EvictionService
from canopy.vole.services.alignment_service import AlignmentService
from canopy.formula_engine.services.formula_service import FormulaService

_BASE = 1_000_000_000_000 - 10_000_000_000  # 10s before clock start


@pytest.fixture
def integrated_client():
    """Client with real Vole -> Formula Engine wiring (no fakes)."""
    config = VoleConfig(max_points=10000, global_bytes_max=10_000_000)
    clock = FakeClock(start_ns=1_000_000_000_000)
    store = PVStore(config)
    alignment = AlignmentService()
    eviction = EvictionService(store, config)
    buffer_service = BufferService(store, alignment, eviction, clock, config)

    data_source = VoleDataSource(buffer_service)
    formula_engine_config = FormulaEngineConfig()
    parser = Parser()
    formula_service = FormulaService(data_source, parser, formula_engine_config)

    vole_app.dependency_overrides[get_buffer_service] = lambda: buffer_service
    formula_engine_app.dependency_overrides[get_formula_service] = lambda: formula_service

    try:
        with TestClient(parent_app) as client:
            yield client, buffer_service
    finally:
        vole_app.dependency_overrides.pop(get_buffer_service, None)
        formula_engine_app.dependency_overrides.pop(get_formula_service, None)


class TestFormulaEngineVoleIntegration:
    async def test_timeseries_formula_with_seeded_data(self, integrated_client):
        client, buffer_service = integrated_client

        samples_a = [
            Sample(ts_ns=_BASE + i * 1000, value=float(i), severity=AlarmSeverity.NO_ALARM, source="archive")
            for i in range(1, 6)
        ]
        samples_b = [
            Sample(ts_ns=_BASE + i * 1000, value=float(i * 2), severity=AlarmSeverity.NO_ALARM, source="archive")
            for i in range(1, 6)
        ]
        await buffer_service.seed("PV:A", samples_a)
        await buffer_service.seed("PV:B", samples_b)

        resp = client.post(
            "/formula-engine/v1/evaluate",
            json={
                "spec": {
                    "kind": "timeseries",
                    "expression": "{a} + {b}",
                    "bindings": {"a": "PV:A", "b": "PV:B"},
                    "align": "last",
                    "index": "union",
                },
                "t0Ns": _BASE + 1000,
                "t1Ns": _BASE + 5000,
            },
            headers=READ_HEADERS,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["kind"] == "timeseries"
        timeseries = data["timeseries"]
        assert len(timeseries["tsNs"]) == 5
        # a + b: [1+2, 2+4, 3+6, 4+8, 5+10] = [3, 6, 9, 12, 15]
        assert timeseries["values"] == [3.0, 6.0, 9.0, 12.0, 15.0]

    async def test_scalar_formula_with_seeded_data(self, integrated_client):
        client, buffer_service = integrated_client

        await buffer_service.seed(
            "PV:X",
            [
                Sample(ts_ns=_BASE + 100, value=7.0, severity=AlarmSeverity.NO_ALARM, source="archive"),
            ],
        )
        await buffer_service.seed(
            "PV:Y",
            [
                Sample(ts_ns=_BASE + 200, value=3.0, severity=AlarmSeverity.NO_ALARM, source="archive"),
            ],
        )

        resp = client.post(
            "/formula-engine/v1/evaluate",
            json={
                "spec": {
                    "kind": "scalar",
                    "expression": "{x} + {y}",
                    "bindings": {"x": "PV:X", "y": "PV:Y"},
                },
                "t0Ns": _BASE,
                "t1Ns": _BASE + 1000,
            },
            headers=READ_HEADERS,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["kind"] == "scalar"
        assert data["scalar"]["value"] == 10.0
