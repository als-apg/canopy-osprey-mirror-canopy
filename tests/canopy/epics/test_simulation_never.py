"""The simulation backend's ``SIM:NEVER:`` names: PVs no IOC serves."""

import asyncio

import pytest

from canopy.epics.base import PVWriteFailed
from canopy.epics.config import EpicsConfig
from canopy.epics.backends.simulation import SimulationConnector


@pytest.mark.parametrize("pv", ["SIM:NEVER:X", "ca://SIM:NEVER:X", "pva://SIM:NEVER:X:ENUM", "SIM:NEVER:X:STR_WF"])
async def test_never_connecting_name_reads_disconnected_and_refuses_writes(pv):
    conn = SimulationConnector.from_config(EpicsConfig())
    value = await conn.get(pv)
    assert value.connected is False
    assert value.value is None
    assert value.pv == pv
    with pytest.raises(PVWriteFailed):
        await conn.put(pv, 1.0)
    assert (await conn.get(pv)).connected is False


async def test_never_connecting_name_describes_as_bare_metadata():
    conn = SimulationConnector.from_config(EpicsConfig())
    meta = await conn.describe("SIM:NEVER:X:WF")
    assert meta.pv == "SIM:NEVER:X:WF"
    assert meta.units is None
    assert meta.enum_choices is None


async def test_never_connecting_monitor_reports_once_then_stays_silent():
    conn = SimulationConnector.from_config(EpicsConfig(sim_update_hz=50.0))
    await conn.start()
    events = []
    sub = await conn.subscribe("SIM:NEVER:X", events.append)
    try:
        await asyncio.sleep(0.2)
    finally:
        await sub.close()
        await conn.close()
    assert len(events) == 1
    assert events[0].connected is False and events[0].value is None


async def test_never_prefix_elsewhere_in_the_name_is_an_ordinary_pv():
    conn = SimulationConnector.from_config(EpicsConfig())
    assert (await conn.get("X:SIM:NEVER:Y")).connected is True


async def _outcomes(conn: SimulationConnector, names: list[str]) -> list[bool]:
    outcomes = []
    for name in names:
        try:
            await conn.put(name, 1.0)
        except PVWriteFailed:
            outcomes.append(False)
        else:
            outcomes.append(True)
    return outcomes


async def test_a_never_connecting_write_draws_the_rng_like_any_other_write():
    """The never-connect refusal comes after the draw, so the seeded outcomes of later writes do not move."""
    plain = [f"SIM:P{i}" for i in range(20)]
    with_never = [*plain[:5], "SIM:NEVER:X", *plain[6:]]
    with_other = [*plain[:5], "SIM:OTHER", *plain[6:]]

    never = await _outcomes(SimulationConnector(put_error_rate=0.5, seed=7), with_never)
    other = await _outcomes(SimulationConnector(put_error_rate=0.5, seed=7), with_other)

    assert never[5] is False
    assert never[:5] + never[6:] == other[:5] + other[6:]


async def test_a_never_connecting_write_fails_even_at_a_zero_error_rate():
    conn = SimulationConnector(put_error_rate=0.0, seed=7)
    with pytest.raises(PVWriteFailed, match="never connects"):
        await conn.put("ca://SIM:NEVER:X", 1.0)
    reading = await conn.get("pva://SIM:NEVER:X")
    assert reading.connected is False
    assert reading.value is None
