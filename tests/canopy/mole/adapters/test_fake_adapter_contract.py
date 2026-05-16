"""Run the adapter contract suite against the FakeArchiverAdapter.

Verifies the contract assertions are themselves correct (the Fake is the
known-good reference) and serves as a template for out-of-tree adapter test
suites.
"""

import pytest

from canopy.mole.adapters.base import PVNotFoundInArchiver

from tests.canopy.mole.adapters import contract
from tests.canopy.mole.adapters._fakes import FakeArchiverAdapter


@pytest.fixture
async def adapter():
    a = FakeArchiverAdapter(name="fake")
    yield a
    await a.close()


async def test_health(adapter):
    await contract.assert_health(adapter)


async def test_close_idempotent():
    fresh = FakeArchiverAdapter(name="fresh")
    await contract.assert_close_idempotent(fresh)


async def test_fetch_window_invariants(adapter):
    await contract.assert_fetch_window_invariants(adapter, pv="PV:TEST", t0_ns=0, t1_ns=10_000_000_000)


async def test_fetch_binned_auto_invariants(adapter):
    await contract.assert_fetch_binned_auto(adapter, pv="PV:TEST", t0_ns=0, t1_ns=10_000_000_000, target_buckets=5)


async def test_unknown_pv_raises(adapter):
    with pytest.raises(PVNotFoundInArchiver):
        await adapter.fetch_window("PV:UNKNOWN", t0_ns=0, t1_ns=1_000)


async def test_normalize_severity_round_trip(adapter):
    """Sanity check that the Fake's severity values pass through normalization."""
    samples = await adapter.fetch_window("PV:TEST", t0_ns=0, t1_ns=10_000_000_000)
    for s in samples:
        assert s.severity.name in {"NO_ALARM", "MINOR", "MAJOR", "INVALID"}
