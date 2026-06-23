"""Channel Access value classification (DBR datatype/count -> ValueKind).

Uses synthetic augmented values (mimicking what aioca's caget/camonitor returns:
the value itself, via ``tolist()``, plus the CA metadata attributes) so the
dispatch + conversion logic is tested deterministically, without a live IOC.
Live CA is covered end-to-end by ``scripts/verify_epics.py``.
"""

import pytest

pytest.importorskip("aioca")
pytest.importorskip("p4p")

from canopy.contracts.pv import ValueKind
from canopy.epics.config import EpicsConfig
from canopy.epics.backends.epics_backend import EpicsConnector

# EPICS DBF field-type codes.
DBR_STRING, DBR_INT, DBR_ENUM, DBR_CHAR, DBR_DOUBLE = 0, 1, 3, 4, 6


class FakeAug:
    """Stand-in for an aioca augmented value: it *is* the value (via ``tolist()``)
    and carries the CA metadata attributes the connector reads."""

    def __init__(self, value, *, datatype, element_count=None, severity=0, timestamp=1.0, enums=None, ok=True):
        self._value = value
        self.datatype = datatype
        self.element_count = (
            element_count if element_count is not None else (len(value) if isinstance(value, list) else 1)
        )
        self.severity = severity
        self.timestamp = timestamp
        self.enums = enums
        self.ok = ok

    def tolist(self):
        return self._value


def _conn(**kwargs) -> EpicsConnector:
    return EpicsConnector.from_config(EpicsConfig(backend="epics", **kwargs))


def test_scalar_dispatch():
    v = _conn()._ca_to_pvvalue("X", FakeAug(3.5, datatype=DBR_DOUBLE))
    assert v.value_kind is ValueKind.SCALAR
    assert v.value == 3.5
    assert v.connected is True


def test_enum_dispatch_keeps_index():
    v = _conn()._ca_to_pvvalue("X", FakeAug(2, datatype=DBR_ENUM, enums=["A", "B", "C"]))
    assert v.value_kind is ValueKind.ENUM
    assert v.value == 2


def test_string_dispatch():
    v = _conn()._ca_to_pvvalue("X", FakeAug("idle", datatype=DBR_STRING))
    assert v.value_kind is ValueKind.STRING
    assert v.value == "idle"


def test_numeric_waveform_dispatch():
    v = _conn()._ca_to_pvvalue("X", FakeAug([1.0, 2.0, 3.0], datatype=DBR_DOUBLE))
    assert v.value_kind is ValueKind.WAVEFORM
    assert v.value == [1.0, 2.0, 3.0]


def test_char_array_decoded_as_string_by_default():
    # "hi\0\0" -> the EPICS long-string idiom decodes to "hi"
    v = _conn()._ca_to_pvvalue("X", FakeAug([104, 105, 0, 0], datatype=DBR_CHAR, element_count=4))
    assert v.value_kind is ValueKind.STRING
    assert v.value == "hi"


def test_char_array_as_waveform_when_disabled():
    v = _conn(char_array_as_string=False)._ca_to_pvvalue("X", FakeAug([104, 105], datatype=DBR_CHAR, element_count=2))
    assert v.value_kind is ValueKind.WAVEFORM
    assert v.value == [104, 105]


def test_waveform_truncated_past_max():
    v = _conn(max_array_elements=3)._ca_to_pvvalue(
        "X", FakeAug([0.0, 1.0, 2.0, 3.0, 4.0], datatype=DBR_DOUBLE, element_count=5)
    )
    assert v.value == [0.0, 1.0, 2.0]
    assert v.error and "truncated" in v.error


def test_disconnected_when_not_ok():
    v = _conn()._ca_to_pvvalue("X", FakeAug(0.0, datatype=DBR_DOUBLE, ok=False))
    assert v.connected is False
