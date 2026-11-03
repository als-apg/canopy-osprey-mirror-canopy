"""Formula Engine pytest fixtures."""

from dataclasses import field, dataclass

import numpy as np
import pytest
from fastapi.testclient import TestClient

from canopy.main import app as parent_app
from canopy.contracts.sample import Sample, AlarmSeverity
from canopy.contracts.formula import IndexMode, AlignmentMode
from canopy.formula_engine.app import app as formula_engine_app
from canopy.formula_engine.ports import AlignedWindow
from canopy.formula_engine.config import FormulaEngineConfig
from canopy.formula_engine.dependencies import get_formula_service
from canopy.formula_engine.services.parser import Parser
from canopy.formula_engine.services.formula_service import FormulaService


@dataclass
class FakeDataSource:
    """Scriptable data source for Formula Engine tests."""

    latest: dict[str, Sample] = field(default_factory=dict)

    async def read_aligned(
        self,
        pvs: list[str],
        t0_ns: int,
        t1_ns: int,
        *,
        align: AlignmentMode,
        index: IndexMode,
        grid_dt_ns: int | None = None,
    ) -> AlignedWindow:
        n = 10
        ts = np.linspace(t0_ns, t1_ns, n, dtype=np.int64)
        arrays: dict[str, np.ndarray] = {}
        severities: dict[str, np.ndarray] = {}
        for i, pv in enumerate(pvs):
            arrays[pv] = np.arange(n, dtype=np.float64) + (i + 1)
            severities[pv] = np.zeros(n, dtype=np.int64)
        return AlignedWindow(ts_ns=ts, arrays=arrays, severities=severities)

    async def read_binned(
        self,
        pvs: list[str],
        t0_ns: int,
        t1_ns: int,
        *,
        target_buckets: int | None = None,
    ) -> AlignedWindow:
        n = target_buckets or 10
        ts = np.linspace(t0_ns, t1_ns, n, dtype=np.int64)
        arrays: dict[str, np.ndarray] = {}
        severities: dict[str, np.ndarray] = {}
        for i, pv in enumerate(pvs):
            arrays[pv] = np.arange(n, dtype=np.float64) + (i + 1)
            severities[pv] = np.zeros(n, dtype=np.int64)
        return AlignedWindow(ts_ns=ts, arrays=arrays, severities=severities)

    async def read_latest(self, pv: str) -> Sample | None:
        return self.latest.get(pv)


@pytest.fixture
def fake_data_source():
    return FakeDataSource(
        latest={
            "PV:A": Sample(ts_ns=1000, value=5.0, severity=AlarmSeverity.NO_ALARM, source="archive"),
            "PV:B": Sample(ts_ns=2000, value=3.0, severity=AlarmSeverity.MINOR, source="archive"),
            "PV:WF": Sample(
                ts_ns=3000, value=bytes([1, 2, 3, 4, 5]), severity=AlarmSeverity.NO_ALARM, source="archive"
            ),
        }
    )


@pytest.fixture
def test_service(fake_data_source):
    """FormulaService backed by FakeDataSource."""
    config = FormulaEngineConfig()
    parser = Parser()
    return FormulaService(fake_data_source, parser, config)


@pytest.fixture
def formula_engine_client(test_service):
    """TestClient with ``get_formula_service`` overridden to use the fake data source.

    Mounted sub-apps maintain their own ``dependency_overrides``, so the override
    must go on ``formula_engine_app`` (not the parent ``canopy.main:app``).
    """
    formula_engine_app.dependency_overrides[get_formula_service] = lambda: test_service
    try:
        with TestClient(parent_app) as client:
            yield client
    finally:
        formula_engine_app.dependency_overrides.pop(get_formula_service, None)
