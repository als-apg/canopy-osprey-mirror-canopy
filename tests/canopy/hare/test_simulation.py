"""Simulation connector backend."""

import pytest

from tests.conftest import wait_for
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
    assert await wait_for(lambda: received), "expected at least one emit"
    await sub.close()
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


async def test_table_value_and_describe():
    conn = SimulationConnector.from_config(EpicsConfig())
    value = await conn.get("SIM:DATA:TABLE")
    assert value.value_kind is ValueKind.TABLE
    table = value.value
    assert table.labels == ["Index", "Value", "Status"]
    assert len(table.columns) == 3
    assert all(len(col) == len(table.columns[0]) for col in table.columns)
    meta = await conn.describe("SIM:DATA:TABLE")
    assert meta.value_kind is ValueKind.TABLE


async def test_scalar_describe_has_value_alarm_limits():
    conn = SimulationConnector.from_config(EpicsConfig())
    meta = await conn.describe("SIM:X")
    assert meta.warning_low == -7.0 and meta.warning_high == 7.0
    assert meta.alarm_low == -9.0 and meta.alarm_high == 9.0


@pytest.mark.parametrize("name", ["SIM:STATE:ENUM", "SIM:X"])
async def test_describe_ignores_the_protocol_prefix(name):
    conn = SimulationConnector.from_config(EpicsConfig())
    bare = await conn.describe(name)
    for spelling in (f"ca://{name}", f"pva://{name}"):
        meta = await conn.describe(spelling)
        assert meta.pv == spelling
        assert meta.model_dump(exclude={"pv"}) == bare.model_dump(exclude={"pv"})


async def test_put_scalar_round_trips():
    conn = SimulationConnector.from_config(EpicsConfig())
    await conn.put("SIM:X", 42.0)
    value = await conn.get("SIM:X")
    assert value.value == 42.0
    assert value.value_kind is ValueKind.SCALAR


async def test_put_string_round_trips():
    conn = SimulationConnector.from_config(EpicsConfig())
    await conn.put("SIM:MSG:STR", "hello")
    value = await conn.get("SIM:MSG:STR")
    assert value.value == "hello"
    assert value.value_kind is ValueKind.STRING


async def test_put_enum_round_trips():
    conn = SimulationConnector.from_config(EpicsConfig())
    await conn.put("SIM:STATE:ENUM", 3)
    value = await conn.get("SIM:STATE:ENUM")
    assert value.value == 3
    assert value.value_kind is ValueKind.ENUM
