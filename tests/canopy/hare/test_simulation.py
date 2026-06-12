"""Simulation connector backend."""

import asyncio

from canopy.contracts.pv import PVValue, ValueKind
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


async def test_scalar_is_default_kind():
    conn = SimulationConnector.from_config(EpicsConfig())
    value = await conn.get("SIM:X")
    assert value.value_kind is ValueKind.SCALAR


async def test_enum_value_and_describe():
    conn = SimulationConnector.from_config(EpicsConfig())
    value = await conn.get("SIM:STATE:ENUM")
    assert value.value_kind is ValueKind.ENUM
    assert isinstance(value.value, int)
    meta = await conn.describe("SIM:STATE:ENUM")
    assert meta.value_kind is ValueKind.ENUM
    assert meta.enum_choices and len(meta.enum_choices) >= 2


async def test_waveform_value_and_describe():
    conn = SimulationConnector(waveform_length=16)
    value = await conn.get("SIM:TRACE:WAVEFORM")
    assert value.value_kind is ValueKind.WAVEFORM
    assert isinstance(value.value, list)
    assert len(value.value) == 16


async def test_string_value():
    conn = SimulationConnector.from_config(EpicsConfig())
    value = await conn.get("SIM:MSG:STR")
    assert value.value_kind is ValueKind.STRING
    assert isinstance(value.value, str)
