"""Simulation backend alarm status and put-failure injection."""

from canopy.epics.config import EpicsConfig
from canopy.contracts.sample import AlarmSeverity
from canopy.epics.backends.simulation import SimulationConnector


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
