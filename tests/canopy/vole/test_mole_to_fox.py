"""End-to-end test: Mole → Vole → Fox.

Fetches archiver data via Mole's gateway, seeds it into Vole, then evaluates
a Fox formula over the buffered data. Exercises the full pipeline using
FakeArchiverAdapter (no real archiver needed).
"""

import pytest
from fastapi.testclient import TestClient

from canopy.main import app as parent_app
from canopy.fox.app import app as fox_app
from tests.conftest import READ_HEADERS
from canopy.mole.app import app as mole_app
from canopy.vole.app import app as vole_app
from canopy.core.clock import FakeClock
from canopy.fox.config import FoxConfig
from canopy.mole.config import MoleConfig
from canopy.vole.config import VoleConfig
from canopy.vole.adapter import VoleDataSource
from canopy.fox.dependencies import get_formula_service
from canopy.mole.dependencies import get_archiver_service
from canopy.vole.buffer.store import PVStore
from canopy.vole.dependencies import get_buffer_service
from canopy.fox.services.parser import Parser
from canopy.mole.adapters.registry import AdapterRegistry
from tests.canopy.mole.adapters._fakes import FakeArchiverAdapter
from canopy.fox.services.formula_service import FormulaService
from canopy.vole.services.buffer_service import BufferService
from canopy.mole.services.archiver_service import ArchiverService
from canopy.vole.services.eviction_service import EvictionService
from canopy.vole.services.alignment_service import AlignmentService


@pytest.fixture
def full_pipeline():
    """Wires Mole (fake archiver) → Vole → Fox in one TestClient."""
    # Mole: fake archiver with two PVs
    fake_adapter = FakeArchiverAdapter(
        name="primary",
        pvs={
            "BEAM:CURR": {"units": "mA", "precision": 2},
            "BEAM:ENERGY": {"units": "GeV", "precision": 4},
        },
    )
    registry = AdapterRegistry({"primary": fake_adapter}, default="primary")
    mole_config = MoleConfig(default_archiver="primary")
    mole_service = ArchiverService(registry, mole_config)

    # Vole: in-memory buffer
    vole_config = VoleConfig(max_age_ns=3_600_000_000_000, max_points=100_000, global_bytes_max=10_000_000)
    clock = FakeClock(start_ns=2_000_000_000_000)
    store = PVStore(vole_config)
    alignment = AlignmentService()
    eviction = EvictionService(store, vole_config)
    buffer_service = BufferService(store, alignment, eviction, clock, vole_config)

    # Fox: wired to Vole
    data_source = VoleDataSource(buffer_service)
    fox_config = FoxConfig()
    parser = Parser()
    formula_service = FormulaService(data_source, parser, fox_config)

    # Override dependencies
    mole_app.dependency_overrides[get_archiver_service] = lambda: mole_service
    vole_app.dependency_overrides[get_buffer_service] = lambda: buffer_service
    fox_app.dependency_overrides[get_formula_service] = lambda: formula_service

    try:
        with TestClient(parent_app) as client:
            yield client, mole_service, buffer_service
    finally:
        mole_app.dependency_overrides.pop(get_archiver_service, None)
        vole_app.dependency_overrides.pop(get_buffer_service, None)
        fox_app.dependency_overrides.pop(get_formula_service, None)


class TestMoleToVoleToFox:
    async def test_fetch_seed_evaluate(self, full_pipeline):
        """Full pipeline: fetch from archiver → seed into Vole → evaluate formula."""
        client, mole_service, buffer_service = full_pipeline

        t0 = 1_000_000_000_000
        t1 = 1_500_000_000_000

        # Step 1: Fetch from Mole (simulates what a future auto-seed would do)
        _, samples_curr, _ = await mole_service.fetch_window("BEAM:CURR", t0, t1)
        _, samples_energy, _ = await mole_service.fetch_window("BEAM:ENERGY", t0, t1)

        assert len(samples_curr) > 0
        assert len(samples_energy) > 0

        # Step 2: Seed into Vole
        count_curr = await buffer_service.seed("BEAM:CURR", samples_curr)
        count_energy = await buffer_service.seed("BEAM:ENERGY", samples_energy)
        assert count_curr > 0
        assert count_energy > 0

        # Step 3: Evaluate a formula via Fox HTTP API
        resp = client.post(
            "/fox/v1/evaluate",
            json={
                "spec": {
                    "kind": "timeseries",
                    "expression": "{curr} + {energy}",
                    "bindings": {"curr": "BEAM:CURR", "energy": "BEAM:ENERGY"},
                    "align": "last",
                    "index": "union",
                },
                "t0Ns": t0,
                "t1Ns": t1,
            },
            headers=READ_HEADERS,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["kind"] == "timeseries"

        timeseries = data["timeseries"]
        assert len(timeseries["tsNs"]) > 0
        assert len(timeseries["values"]) == len(timeseries["tsNs"])
        assert all(v is not None for v in timeseries["values"])

    async def test_scalar_formula_after_seed(self, full_pipeline):
        """Scalar eval uses read_latest, which returns the last seeded sample."""
        client, mole_service, buffer_service = full_pipeline

        t0 = 1_000_000_000_000
        t1 = 1_500_000_000_000

        _, samples, _ = await mole_service.fetch_window("BEAM:CURR", t0, t1)
        await buffer_service.seed("BEAM:CURR", samples)

        resp = client.post(
            "/fox/v1/evaluate",
            json={
                "spec": {
                    "kind": "scalar",
                    "expression": "{x} * 2",
                    "bindings": {"x": "BEAM:CURR"},
                },
                "t0Ns": t0,
                "t1Ns": t1,
            },
            headers=READ_HEADERS,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["kind"] == "scalar"
        # Last sample value from fake adapter is float(n-1) = 9.0, so 9.0 * 2 = 18.0
        assert data["scalar"]["value"] == 18.0

    async def test_pv_not_buffered_returns_422(self, full_pipeline):
        """Fox returns FormulaInputMissing when Vole has no data for a PV."""
        client, _, _ = full_pipeline

        resp = client.post(
            "/fox/v1/evaluate",
            json={
                "spec": {
                    "kind": "scalar",
                    "expression": "{x}",
                    "bindings": {"x": "PV:NOT:SEEDED"},
                },
                "t0Ns": 0,
                "t1Ns": 1000,
            },
            headers=READ_HEADERS,
        )
        assert resp.status_code == 422
        assert resp.json()["code"] == "formula_input_missing"
