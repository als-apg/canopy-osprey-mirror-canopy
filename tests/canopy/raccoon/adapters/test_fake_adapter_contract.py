"""Run the adapter contract suite against the FakeDirectoryAdapter.

Verifies the contract assertions are themselves correct (the Fake is the
known-good reference) and serves as a template for out-of-tree adapter test
suites.
"""

import pytest

from tests.canopy.raccoon.adapters import contract
from tests.canopy.raccoon.adapters._fakes import FakeDirectoryAdapter


@pytest.fixture
async def adapter():
    a = FakeDirectoryAdapter()
    a.pvs = {"PV:TEST:1", "PV:TEST:2"}
    a.devices = {"DEV:TEST"}
    a.elements = {"EL:TEST"}
    yield a
    await a.close()


async def test_identity_attributes(adapter):
    await contract.assert_identity_attributes(adapter)


async def test_health(adapter):
    await contract.assert_health(adapter)


async def test_close_idempotent():
    fresh = FakeDirectoryAdapter(name="fresh")
    await contract.assert_close_idempotent(fresh)


async def test_list_pvs_invariants(adapter):
    await contract.assert_list_pvs_invariants(adapter, pattern="PV")


async def test_list_devices_invariants(adapter):
    await contract.assert_list_devices_invariants(adapter, pattern="DEV")


async def test_list_elements_invariants(adapter):
    await contract.assert_list_elements_invariants(adapter, pattern="EL")
