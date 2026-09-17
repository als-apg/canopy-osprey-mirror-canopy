"""Channel Access value classification (DBR datatype/count -> ValueKind).

Uses synthetic augmented values (mimicking what aioca's caget/camonitor returns:
the value itself, via ``tolist()``, plus the CA metadata attributes) so the
dispatch + conversion logic is tested deterministically, without a live IOC.
Live CA is covered end-to-end by ``scripts/verify_epics.py``.
"""

import pytest

pytest.importorskip("aioca")
pytest.importorskip("p4p")

from canopy.epics.base import PVWriteFailed
from canopy.contracts.pv import ValueKind
from canopy.epics.config import EpicsConfig
from canopy.epics.backends import epics_backend
from canopy.epics.backends.epics_backend import EpicsConnector

# EPICS DBF field-type codes.
DBR_STRING, DBR_INT, DBR_ENUM, DBR_CHAR, DBR_DOUBLE = 0, 1, 3, 4, 6


class FakeAug:
    """Stand-in for an aioca augmented value: it *is* the value (via ``tolist()``)
    and carries the CA metadata attributes the connector reads."""

    def __init__(
        self,
        value,
        *,
        datatype,
        element_count=None,
        severity=0,
        status=0,
        timestamp=1.0,
        enums=None,
        ok=True,
    ):
        self._value = value
        self.datatype = datatype
        self.element_count = (
            element_count if element_count is not None else (len(value) if isinstance(value, list) else 1)
        )
        self.severity = severity
        self.status = status
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


# --- alarm status ----------------------------------------------------------- #


def test_alarm_status_carried_from_augmented_value():
    v = _conn()._ca_to_pvvalue("X", FakeAug(9.5, datatype=DBR_DOUBLE, severity=2, status=3))
    assert v.alarm_status == 3


def test_healthy_value_reports_status_zero():
    assert _conn()._ca_to_pvvalue("X", FakeAug(1.0, datatype=DBR_DOUBLE)).alarm_status == 0


def test_alarm_status_none_when_augmented_value_lacks_it():
    aug = FakeAug(1.0, datatype=DBR_DOUBLE)
    del aug.status
    assert _conn()._ca_to_pvvalue("X", aug).alarm_status is None


def test_disconnected_value_has_no_alarm_status():
    v = _conn()._ca_to_pvvalue("X", FakeAug(0.0, datatype=DBR_DOUBLE, ok=False))
    assert v.alarm_status is None


@pytest.mark.parametrize(
    "raw,expected",
    [(None, None), (3, 3), ("4", 4), (5.0, 5), ("bogus", None), (object(), None)],
)
def test_alarm_status_coercion(raw, expected):
    assert epics_backend._alarm_status(raw) == expected


# --- writes (put) ----------------------------------------------------------- #


class FakeCANothing:
    """Stand-in for aioca's CANothing put result, carrying the `.ok` flag the
    connector checks."""

    def __init__(self, ok=True):
        self.ok = ok

    def __repr__(self):
        return f"FakeCANothing(ok={self.ok})"


def _fake_caput(calls, *, ok=True, raises=None):
    """Build an async `caput` replacement that records its call and returns a
    FakeCANothing (or raises)."""

    async def fake_caput(name, value, **kwargs):
        calls.append((name, value, kwargs))
        if raises is not None:
            raise raises
        return FakeCANothing(ok=ok)

    return fake_caput


async def test_put_ca_routes_and_passes_args(monkeypatch):
    calls = []
    monkeypatch.setattr(epics_backend, "caput", _fake_caput(calls))
    await _conn().put("PV:X", 5.0)
    assert len(calls) == 1
    name, value, kwargs = calls[0]
    assert name == "PV:X"
    assert value == 5.0
    assert kwargs["wait"] is True
    # No explicit timeout -> defaults to the connector's ca_timeout_s.
    assert kwargs["timeout"] == EpicsConfig().ca_timeout_s
    assert kwargs["throw"] is False


async def test_put_ca_strips_prefix(monkeypatch):
    calls = []
    monkeypatch.setattr(epics_backend, "caput", _fake_caput(calls))
    await _conn().put("ca://PV:X", 1)
    assert calls[0][0] == "PV:X"


async def test_put_ca_honors_timeout_and_wait(monkeypatch):
    calls = []
    monkeypatch.setattr(epics_backend, "caput", _fake_caput(calls))
    await _conn().put("PV:X", 2, timeout=0.5, wait=False)
    _, _, kwargs = calls[0]
    assert kwargs["timeout"] == 0.5
    assert kwargs["wait"] is False


async def test_put_ca_not_ok_raises(monkeypatch):
    monkeypatch.setattr(epics_backend, "caput", _fake_caput([], ok=False))
    with pytest.raises(PVWriteFailed):
        await _conn().put("PV:X", 3.0)


async def test_put_ca_exception_raises(monkeypatch):
    monkeypatch.setattr(epics_backend, "caput", _fake_caput([], raises=RuntimeError("boom")))
    with pytest.raises(PVWriteFailed):
        await _conn().put("PV:X", 3.0)
