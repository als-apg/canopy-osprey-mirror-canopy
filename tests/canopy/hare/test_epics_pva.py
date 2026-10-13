"""Regression tests for the EPICS connector's PV Access (``pva://``) path."""

import pytest

pytest.importorskip("p4p.server")

from p4p import Type, Value
from p4p.nt import NTScalar
from p4p.server import Server
from p4p.server.thread import SharedPV

from tests.conftest import wait_for
from canopy.epics.base import PVWriteFailed
from canopy.contracts.pv import ValueKind
from canopy.epics.config import EpicsConfig
from canopy.epics.backends import epics_backend
from canopy.epics.backends.epics_backend import EpicsConnector

PV_NAME = "CANOPY:TEST:PVA"


@pytest.fixture
def pva_server(monkeypatch):
    """An in-process p4p PVA server hosting one NTScalar, confined to loopback."""
    # Loopback only, on non-standard ports
    monkeypatch.setenv("EPICS_PVA_ADDR_LIST", "127.0.0.1")
    monkeypatch.setenv("EPICS_PVA_AUTO_ADDR_LIST", "NO")
    monkeypatch.setenv("EPICS_PVA_SERVER_PORT", "5085")
    monkeypatch.setenv("EPICS_PVA_BROADCAST_PORT", "5086")
    pv = SharedPV(nt=NTScalar("d"), initial=1.0)
    server = Server(providers=[{PV_NAME: pv}])
    try:
        yield pv
    finally:
        server.stop()


async def test_pva_get(pva_server):
    conn = EpicsConnector.from_config(EpicsConfig(backend="epics", pva_timeout_s=5.0))
    await conn.start()
    try:
        value = await conn.get(f"pva://{PV_NAME}")
        assert value.connected is True
        assert value.value == 1.0
        # A server without an explicit timestamp reports secondsPastEpoch=0
        assert value.ts_ns > 0
        assert value.alarm_status == 0
    finally:
        await conn.close()


@pytest.mark.parametrize("message,expected", [("HIHI", 3), ("LOLO", 5), ("pump tripped", None)])
async def test_pva_alarm_status_comes_from_the_alarm_message(pva_server, message, expected):
    pva_server.post(9.5, severity=2, message=message)
    conn = EpicsConnector.from_config(EpicsConfig(backend="epics", pva_timeout_s=5.0))
    await conn.start()
    try:
        value = await conn.get(f"pva://{PV_NAME}")
        assert value.value == 9.5
        assert value.alarm_status == expected
    finally:
        await conn.close()


def _alarm_value(severity=None, status=None, message=None):
    """A p4p NTScalar carrying the given alarm fields, so the dotted-path lookups go through p4p itself."""
    kwargs = {}
    if severity is not None:
        kwargs["severity"] = severity
    if message is not None:
        kwargs["message"] = message
    value = NTScalar("d").wrap(1.0, **kwargs)
    if status is not None:
        # wrap() has no status argument, so set the category directly
        value["alarm.status"] = status
    return value


@pytest.mark.parametrize(
    "severity,status,message,expected",
    [
        (2, 1, "HIHI", 3),
        (1, 1, "LOW_ALARM", 6),
        (0, 0, "", 0),
        (2, 1, "", None),
        (2, 1, "pump tripped", None),
        # A message with no severity supplied by the caller
        (None, None, "HIHI", 3),
    ],
)
def test_pva_alarm_status_reads_the_message_not_the_category(severity, status, message, expected):
    value = _alarm_value(severity, status, message)
    assert epics_backend._pva_alarm_status(value, severity) == expected


def test_pva_alarm_status_is_none_without_an_alarm_structure():
    value = Value(Type([("value", "d")]), {"value": 1.0})
    assert epics_backend._pva_alarm_status(value, None) is None


@pytest.mark.parametrize(
    "severity,status,message,expected",
    [
        (2, 1, "NO_ALARM", None),
        (0, 0, "LOW", 0),
        (0, 3, "HIHI", None),
    ],
)
def test_pva_alarm_status_ignores_a_message_that_contradicts_severity(severity, status, message, expected):
    value = _alarm_value(severity, status, message)
    assert epics_backend._pva_alarm_status(value, severity) == expected


async def test_pva_monitor_delivers_updates(pva_server):
    conn = EpicsConnector.from_config(EpicsConfig(backend="epics", pva_timeout_s=5.0))
    await conn.start()
    received = []
    try:
        sub = await conn.subscribe(f"pva://{PV_NAME}", received.append)
        assert await wait_for(lambda: len(received) >= 1), "no initial value from monitor"
        assert received[0].connected is True
        pva_server.post(2.0)
        assert await wait_for(lambda: any(r.value == 2.0 for r in received)), "posted update not delivered"
        await sub.close()
    finally:
        await conn.close()


@pytest.fixture
def pva_typed_server(monkeypatch):
    """In-process p4p server hosting one of each dispatch case — NTEnum,
    NTScalarArray, NTNDArray, NTTable, and an NTScalar with valueAlarm limits —
    so the connector's Normative-Type dispatch + metadata can be exercised.
    Loopback only."""
    import numpy as np
    from p4p.nt import NTEnum, NTTable, NTNDArray

    monkeypatch.setenv("EPICS_PVA_ADDR_LIST", "127.0.0.1")
    monkeypatch.setenv("EPICS_PVA_AUTO_ADDR_LIST", "NO")
    monkeypatch.setenv("EPICS_PVA_SERVER_PORT", "5085")
    monkeypatch.setenv("EPICS_PVA_BROADCAST_PORT", "5086")

    enum_pv = SharedPV(nt=NTEnum(), initial={"index": 1, "choices": ["OFF", "ON", "FAULT"]})
    array_pv = SharedPV(nt=NTScalar("ad"), initial=[1.0, 2.0, 3.0, 4.0])
    image_pv = SharedPV(nt=NTNDArray(), initial=np.zeros((4, 4), dtype=np.uint8))
    table_nt = NTTable(columns=[("idx", "i"), ("name", "s"), ("val", "d")])
    table_pv = SharedPV(
        nt=table_nt,
        initial=[
            {"idx": 1, "name": "a", "val": 1.5},
            {"idx": 2, "name": "b", "val": 2.5},
            {"idx": 3, "name": "c", "val": 3.5},
        ],
    )
    alarm_nt = NTScalar("d", valueAlarm=True, display=True)
    alarm_pv = SharedPV(
        nt=alarm_nt,
        initial=alarm_nt.wrap(
            {
                "value": 5.0,
                "valueAlarm": {
                    "lowWarningLimit": 1.0,
                    "highWarningLimit": 9.0,
                    "lowAlarmLimit": 0.0,
                    "highAlarmLimit": 10.0,
                },
                "display": {"units": "A"},
            }
        ),
    )
    server = Server(
        providers=[
            {
                "CANOPY:TEST:ENUM": enum_pv,
                "CANOPY:TEST:ARRAY": array_pv,
                "CANOPY:TEST:IMAGE": image_pv,
                "CANOPY:TEST:TABLE": table_pv,
                "CANOPY:TEST:ALARM": alarm_pv,
            }
        ]
    )
    try:
        yield
    finally:
        server.stop()


async def test_pva_enum_dispatch_and_choices(pva_typed_server):
    conn = EpicsConnector.from_config(EpicsConfig(backend="epics"))
    await conn.start()
    try:
        value = await conn.get("pva://CANOPY:TEST:ENUM")
        assert value.connected is True
        assert value.value_kind is ValueKind.ENUM
        assert value.value == 1  # the index; choices come from describe()
        meta = await conn.describe("pva://CANOPY:TEST:ENUM")
        assert meta.value_kind is ValueKind.ENUM
        assert meta.enum_choices == ["OFF", "ON", "FAULT"]
    finally:
        await conn.close()


async def test_pva_waveform_dispatch(pva_typed_server):
    conn = EpicsConnector.from_config(EpicsConfig(backend="epics"))
    await conn.start()
    try:
        value = await conn.get("pva://CANOPY:TEST:ARRAY")
        assert value.value_kind is ValueKind.WAVEFORM
        assert list(value.value) == [1.0, 2.0, 3.0, 4.0]
    finally:
        await conn.close()


async def test_pva_image_reported_unsupported(pva_typed_server):
    conn = EpicsConnector.from_config(EpicsConfig(backend="epics"))
    await conn.start()
    try:
        value = await conn.get("pva://CANOPY:TEST:IMAGE")
        assert value.value_kind is ValueKind.UNSUPPORTED
        assert value.value is None
        assert value.error and "NTNDArray" in value.error
    finally:
        await conn.close()


async def test_pva_table_dispatch(pva_typed_server):
    conn = EpicsConnector.from_config(EpicsConfig(backend="epics"))
    await conn.start()
    try:
        value = await conn.get("pva://CANOPY:TEST:TABLE")
        assert value.value_kind is ValueKind.TABLE
        table = value.value
        assert table.labels == ["idx", "name", "val"]
        assert table.columns == [[1, 2, 3], ["a", "b", "c"], [1.5, 2.5, 3.5]]
    finally:
        await conn.close()


async def test_pva_value_alarm_limits_in_describe(pva_typed_server):
    conn = EpicsConnector.from_config(EpicsConfig(backend="epics"))
    await conn.start()
    try:
        meta = await conn.describe("pva://CANOPY:TEST:ALARM")
        assert meta.warning_low == 1.0
        assert meta.warning_high == 9.0
        assert meta.alarm_low == 0.0
        assert meta.alarm_high == 10.0
        assert meta.units == "A"
    finally:
        await conn.close()


# --- writes (put) ----------------------------------------------------------- #


class _AcceptPut:
    """A SharedPV handler that accepts puts by posting the new value, so a write
    actually lands and a subsequent get reflects it (a default SharedPV rejects
    puts)."""

    def put(self, pv, op):
        pv.post(op.value())
        op.done()


@pytest.fixture
def pva_writable_server(monkeypatch):
    """In-process p4p server hosting one writable NTScalar, loopback only."""
    monkeypatch.setenv("EPICS_PVA_ADDR_LIST", "127.0.0.1")
    monkeypatch.setenv("EPICS_PVA_AUTO_ADDR_LIST", "NO")
    monkeypatch.setenv("EPICS_PVA_SERVER_PORT", "5085")
    monkeypatch.setenv("EPICS_PVA_BROADCAST_PORT", "5086")
    pv = SharedPV(nt=NTScalar("d"), initial=1.0, handler=_AcceptPut())
    server = Server(providers=[{PV_NAME: pv}])
    try:
        yield pv
    finally:
        server.stop()


async def test_pva_put_round_trips(pva_writable_server):
    conn = EpicsConnector.from_config(EpicsConfig(backend="epics", pva_timeout_s=5.0))
    await conn.start()
    try:
        await conn.put(f"pva://{PV_NAME}", 7.0)
        value = await conn.get(f"pva://{PV_NAME}")
        assert value.connected is True
        assert value.value == 7.0
    finally:
        await conn.close()


async def test_pva_put_timeout_raises():
    # No server hosts this PV, so a put with a tiny timeout cannot complete.
    conn = EpicsConnector.from_config(EpicsConfig(backend="epics"))
    await conn.start()
    try:
        with pytest.raises(PVWriteFailed):
            await conn.put("pva://CANOPY:TEST:NOPE", 1.0, timeout=0.2)
    finally:
        await conn.close()
