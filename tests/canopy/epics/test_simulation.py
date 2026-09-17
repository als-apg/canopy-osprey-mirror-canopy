"""Simulation backend alarm status and put-failure injection."""

import random

import pytest
from pydantic import ValidationError

from canopy.epics.base import PVWriteFailed
from canopy.epics.config import EpicsConfig
from canopy.contracts.sample import AlarmSeverity
from canopy.epics.backends.simulation import SimulationConnector, _parse_fail_pvs


def test_status_matches_severity():
    assert SimulationConnector._status_for(AlarmSeverity.NO_ALARM) == 0
    assert SimulationConnector._status_for(AlarmSeverity.MINOR) == 4
    assert SimulationConnector._status_for(AlarmSeverity.MAJOR) == 3
    assert SimulationConnector._status_for(AlarmSeverity.INVALID) == 17


def test_every_severity_has_a_status():
    assert {SimulationConnector._status_for(s) for s in AlarmSeverity} == {0, 3, 4, 17}


async def test_scalar_get_carries_status_matching_its_severity():
    conn = SimulationConnector.from_config(EpicsConfig())
    value = await conn.get("SIM:X")
    assert value.alarm_status == SimulationConnector._status_for(value.severity)


async def test_every_value_kind_carries_a_status():
    conn = SimulationConnector.from_config(EpicsConfig())
    for pv in ("SIM:X", "SIM:STATE:ENUM", "SIM:TRACE:WAVEFORM", "SIM:MSG:STR", "SIM:DATA:TABLE"):
        value = await conn.get(pv)
        assert value.alarm_status == SimulationConnector._status_for(value.severity), pv


async def test_written_value_reports_no_alarm_status():
    conn = SimulationConnector.from_config(EpicsConfig())
    await conn.put("SIM:X", 42.0)
    value = await conn.get("SIM:X")
    assert value.severity is AlarmSeverity.NO_ALARM
    assert value.alarm_status == 0


# --- put failure injection -------------------------------------------------- #


async def test_default_rate_never_fails():
    conn = SimulationConnector()
    for i in range(50):
        await conn.put("SIM:X", float(i))
    assert (await conn.get("SIM:X")).value == 49.0


async def test_rate_one_always_fails():
    conn = SimulationConnector(put_error_rate=1.0)
    for _ in range(20):
        with pytest.raises(PVWriteFailed):
            await conn.put("SIM:X", 1.0)


async def test_failed_put_leaves_no_override():
    conn = SimulationConnector(put_error_rate=1.0)
    with pytest.raises(PVWriteFailed):
        await conn.put("SIM:MSG:STR", "written")
    assert (await conn.get("SIM:MSG:STR")).value != "written"


async def test_named_pv_fails_even_at_rate_zero():
    conn = SimulationConnector(put_fail_pvs=frozenset({"SIM:BAD"}))
    with pytest.raises(PVWriteFailed) as excinfo:
        await conn.put("SIM:BAD", 1.0)
    assert "SIM:BAD" in str(excinfo.value)
    await conn.put("SIM:GOOD", 1.0)
    assert (await conn.get("SIM:GOOD")).value == 1.0


async def _put_outcomes(conn: SimulationConnector, count: int = 20) -> list[bool]:
    """Whether each of `count` successive puts to distinct PVs succeeded."""
    outcomes = []
    for i in range(count):
        try:
            await conn.put(f"SIM:P{i}", float(i))
            outcomes.append(True)
        except PVWriteFailed:
            outcomes.append(False)
    return outcomes


async def test_same_seed_reproduces_the_failure_sequence():
    first = await _put_outcomes(SimulationConnector(put_error_rate=0.5, seed=1234))
    second = await _put_outcomes(SimulationConnector(put_error_rate=0.5, seed=1234))
    assert first == second
    assert False in first and True in first


async def test_different_seeds_diverge():
    first = await _put_outcomes(SimulationConnector(put_error_rate=0.5, seed=1234))
    second = await _put_outcomes(SimulationConnector(put_error_rate=0.5, seed=4321))
    assert first != second


async def test_failure_rng_is_private_to_the_instance():
    conn = SimulationConnector(put_error_rate=0.5, seed=7)
    baseline = await _put_outcomes(SimulationConnector(put_error_rate=0.5, seed=7))
    random.seed(0)
    assert await _put_outcomes(conn) == baseline


# --- configuration ---------------------------------------------------------- #


def test_from_config_wires_failure_settings():
    cfg = EpicsConfig(sim_put_error_rate=0.25, sim_put_fail_pvs="SIM:A, SIM:B", sim_seed=99)
    conn = SimulationConnector.from_config(cfg)
    assert conn._put_error_rate == 0.25
    assert conn._put_fail_pvs == frozenset({"SIM:A", "SIM:B"})


async def test_from_config_seed_is_applied():
    cfg = EpicsConfig(sim_put_error_rate=0.5, sim_seed=99)
    assert await _put_outcomes(SimulationConnector.from_config(cfg)) == await _put_outcomes(
        SimulationConnector(put_error_rate=0.5, seed=99)
    )


def test_failure_injection_defaults_to_off():
    cfg = EpicsConfig()
    assert cfg.sim_put_error_rate == 0.0
    assert cfg.sim_put_fail_pvs == ""
    assert cfg.sim_seed is None


@pytest.mark.parametrize("raw,expected", [("", set()), ("A", {"A"}), (" A , B ,,", {"A", "B"}), (",", set())])
def test_parse_fail_pvs(raw, expected):
    assert _parse_fail_pvs(raw) == frozenset(expected)


@pytest.mark.parametrize("rate", [1.5, -0.1])
def test_put_error_rate_out_of_range_rejected(rate):
    with pytest.raises(ValidationError):
        EpicsConfig(sim_put_error_rate=rate)
