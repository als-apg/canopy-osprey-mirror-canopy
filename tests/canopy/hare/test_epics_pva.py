"""Regression tests for the EPICS connector's PV Access (``pva://``) path."""

import asyncio

import pytest

pytest.importorskip("p4p.server")

from p4p.nt import NTScalar
from p4p.server import Server
from p4p.server.thread import SharedPV

from canopy.contracts.pv import ValueKind
from canopy.epics.config import EpicsConfig
from canopy.epics.backends.epics_backend import EpicsConnector

PV_NAME = "CANOPY:TEST:PVA"


@pytest.fixture
def pva_server(monkeypatch):
    """An in-process p4p PVA server hosting one NTScalar, confined to loopback."""
    # Loopback only, on non-standard ports
    monkeypatch.setenv("EPICS_PVA_ADDR_LIST", "127.0.0.1")
    monkeypatch.setenv("EPICS_PVA_AUTO_ADDR_LIST", "NO")
    monkeypatch.setenv("EPICS_PVA_SERVER_PORT", "5085")
    monkeypatch.setenv("EPICS_PVA_BROADCAST_PORT", "5086")
    pv = SharedPV(nt=NTScalar("d"), initial=1.0)
    server = Server(providers=[{PV_NAME: pv}])
    try:
        yield pv
    finally:
        server.stop()


async def _wait_for(predicate, timeout: float = 5.0, interval: float = 0.05) -> bool:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(interval)
    return False


async def test_pva_get(pva_server):
    conn = EpicsConnector.from_config(EpicsConfig(backend="epics", pva_timeout_s=5.0))
    await conn.start()
    try:
        value = await conn.get(f"pva://{PV_NAME}")
        assert value.connected is True
        assert value.value == 1.0
        # A server without an explicit timestamp reports secondsPastEpoch=0
        assert value.ts_ns > 0
    finally:
        await conn.close()


async def test_pva_monitor_delivers_updates(pva_server):
    conn = EpicsConnector.from_config(EpicsConfig(backend="epics", pva_timeout_s=5.0))
    await conn.start()
    received = []
    try:
        sub = await conn.subscribe(f"pva://{PV_NAME}", received.append)
        assert await _wait_for(lambda: len(received) >= 1), "no initial value from monitor"
        assert received[0].connected is True
        pva_server.post(2.0)
        assert await _wait_for(lambda: any(r.value == 2.0 for r in received)), "posted update not delivered"
        await sub.close()
    finally:
        await conn.close()


@pytest.fixture
def pva_typed_server(monkeypatch):
    """In-process p4p server hosting an NTEnum, an NTScalarArray, and an NTNDArray
    so the connector's Normative-Type dispatch can be exercised. Loopback only."""
    import numpy as np
    from p4p.nt import NTEnum, NTNDArray

    monkeypatch.setenv("EPICS_PVA_ADDR_LIST", "127.0.0.1")
    monkeypatch.setenv("EPICS_PVA_AUTO_ADDR_LIST", "NO")
    monkeypatch.setenv("EPICS_PVA_SERVER_PORT", "5085")
    monkeypatch.setenv("EPICS_PVA_BROADCAST_PORT", "5086")

    enum_pv = SharedPV(nt=NTEnum(), initial={"index": 1, "choices": ["OFF", "ON", "FAULT"]})
    array_pv = SharedPV(nt=NTScalar("ad"), initial=[1.0, 2.0, 3.0, 4.0])
    image_pv = SharedPV(nt=NTNDArray(), initial=np.zeros((4, 4), dtype=np.uint8))
    server = Server(
        providers=[
            {
                "CANOPY:TEST:ENUM": enum_pv,
                "CANOPY:TEST:ARRAY": array_pv,
                "CANOPY:TEST:IMAGE": image_pv,
            }
        ]
    )
    try:
        yield
    finally:
        server.stop()


async def test_pva_enum_dispatch_and_choices(pva_typed_server):
    conn = EpicsConnector.from_config(EpicsConfig(backend="epics"))
    await conn.start()
    try:
        value = await conn.get("pva://CANOPY:TEST:ENUM")
        assert value.connected is True
        assert value.value_kind is ValueKind.ENUM
        assert value.value == 1  # the index; choices come from describe()
        meta = await conn.describe("pva://CANOPY:TEST:ENUM")
        assert meta.value_kind is ValueKind.ENUM
        assert meta.enum_choices == ["OFF", "ON", "FAULT"]
    finally:
        await conn.close()


async def test_pva_waveform_dispatch(pva_typed_server):
    conn = EpicsConnector.from_config(EpicsConfig(backend="epics"))
    await conn.start()
    try:
        value = await conn.get("pva://CANOPY:TEST:ARRAY")
        assert value.value_kind is ValueKind.WAVEFORM
        assert list(value.value) == [1.0, 2.0, 3.0, 4.0]
    finally:
        await conn.close()


async def test_pva_image_reported_unsupported(pva_typed_server):
    conn = EpicsConnector.from_config(EpicsConfig(backend="epics"))
    await conn.start()
    try:
        value = await conn.get("pva://CANOPY:TEST:IMAGE")
        assert value.value_kind is ValueKind.UNSUPPORTED
        assert value.value is None
        assert value.error and "NTNDArray" in value.error
    finally:
        await conn.close()
