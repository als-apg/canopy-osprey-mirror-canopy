"""Regression tests for the EPICS connector's PV Access (``pva://``) path."""

import asyncio

import pytest

pytest.importorskip("p4p.server")

from p4p.nt import NTScalar
from p4p.server import Server
from p4p.server.thread import SharedPV

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
