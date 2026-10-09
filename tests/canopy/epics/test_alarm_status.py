"""EPICS ``menuAlarmStat`` codes, shared by every backend's ``PVValue.alarm_status``."""

import pytest

from canopy.epics.base import ALARM_STATUS_CODES, alarm_status_from_name


def test_codes_follow_menu_alarm_stat_order():
    assert ALARM_STATUS_CODES["NO_ALARM"] == 0
    assert ALARM_STATUS_CODES["HIHI"] == 3
    assert ALARM_STATUS_CODES["LOLO"] == 5
    assert ALARM_STATUS_CODES["STATE"] == 7
    assert ALARM_STATUS_CODES["UDF"] == 17
    assert ALARM_STATUS_CODES["WRITE_ACCESS"] == 21
    assert sorted(ALARM_STATUS_CODES.values()) == list(range(22))


@pytest.mark.parametrize(
    "name,code",
    [
        ("HIHI", 3),
        ("hihi", 3),
        (" LOW ", 6),
        ("HIHI_ALARM", 3),
        ("NO_ALARM", 0),
        ("udf_alarm", 17),
    ],
)
def test_known_names_map_to_their_code(name, code):
    assert alarm_status_from_name(name) == code


@pytest.mark.parametrize("name", ["", "pump tripped", "_ALARM", None, 3])
def test_anything_else_has_no_code(name):
    assert alarm_status_from_name(name) is None
