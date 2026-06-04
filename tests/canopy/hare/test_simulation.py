"""Simulation connector backend."""

import asyncio

from canopy.contracts.pv import PVValue
from canopy.epics.config import EpicsConfig
from canopy.epics.backends.simulation import SimulationConnector


async def test_get_returns_value():
    conn = SimulationConnector.from_config(EpicsConfig())
    value = await conn.get("SIM:X")
    assert isinstance(value, PVValue)
    assert value.pv == "SIM:X"
    assert value.connected is True
    assert isinstance(value.value, float)


async def test_subscribe_emits_and_closes():
    conn = SimulationConnector(update_hz=50.0)
    received: list[PVValue] = []
    sub = await conn.subscribe("SIM:Y", received.append)
    await asyncio.sleep(0.05)  # let the monitor task emit a few times
    await sub.close()
    assert received, "expected at least one emit"
    assert all(r.pv == "SIM:Y" for r in received)
    await conn.close()
