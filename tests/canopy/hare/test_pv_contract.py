"""Wire-contract checks for the live-PV types (camelCase aliasing + valueKind)."""

from canopy.contracts.pv import PVMeta, PVValue, ValueKind
from canopy.contracts.sample import AlarmSeverity


def test_pvvalue_defaults_scalar_and_camelcases():
    v = PVValue(pv="X", value=1.0, severity=AlarmSeverity.NO_ALARM, ts_ns=5)
    dumped = v.model_dump(by_alias=True)
    assert dumped["valueKind"] == "scalar"
    assert dumped["tsNs"] == 5


def test_pvmeta_camelcases_all_fields():
    m = PVMeta(
        pv="X",
        value_kind=ValueKind.ENUM,
        enum_choices=["A", "B"],
        display_low=-1.0,
        display_high=1.0,
        control_low=-2.0,
        control_high=2.0,
        writable=True,
    )
    dumped = m.model_dump(by_alias=True)
    assert dumped["valueKind"] == "enum"
    assert dumped["enumChoices"] == ["A", "B"]
    assert dumped["displayLow"] == -1.0
    assert dumped["controlHigh"] == 2.0
    assert dumped["writable"] is True
