"""Unit tests for the EPICS adapter: PVValue->EpicsValue mapping + batch get/put.

A fake connector stands in for ``canopy.epics`` — no IOCs, no DB.
"""

from typing import Any
from datetime import UTC, datetime

from canopy.contracts.pv import PVValue
from canopy.contracts.sample import AlarmSeverity
from canopy.squirrel.adapters.epics_io import EpicsIO, _to_epics_value


def _pv(addr: str, value: Any, severity=AlarmSeverity.NO_ALARM, ts_ns: int = 1_000_000_000) -> PVValue:
    return PVValue(pv=addr, value=value, severity=severity, ts_ns=ts_ns)


class FakeConnector:
    def __init__(self, values: dict[str, PVValue] | None = None, fail: set[str] | None = None):
        self.values = values or {}
        self.fail = fail or set()
        self.puts: list[tuple[str, Any]] = []

    async def get(self, addr: str, *, timeout=None) -> PVValue:
        if addr in self.fail:
            raise RuntimeError(f"read boom {addr}")
        return self.values[addr]

    async def put(self, addr: str, value: Any, *, timeout=None, wait: bool = True) -> None:
        if addr in self.fail:
            raise RuntimeError(f"write boom {addr}")
        self.puts.append((addr, value))


# --- mapping ---------------------------------------------------------------- #


def test_maps_severity_code_and_aware_timestamp():
    ev = _to_epics_value(_pv("X", 42.0, AlarmSeverity.MAJOR, ts_ns=1_500_000_000))
    assert ev.value == 42.0
    assert ev.severity == 2  # MAJOR -> integer code
    assert ev.timestamp == datetime.fromtimestamp(1.5, tz=UTC)
    assert ev.timestamp.tzinfo is not None
    assert ev.connected is True
    assert ev.units is None and ev.precision is None  # v1 leaves these unset


def test_zero_timestamp_becomes_none():
    assert _to_epics_value(_pv("X", 1, ts_ns=0)).timestamp is None


def test_severity_codes():
    codes = {sev: _to_epics_value(_pv("X", 0, sev)).severity for sev in AlarmSeverity}
    assert codes == {
        AlarmSeverity.NO_ALARM: 0,
        AlarmSeverity.MINOR: 1,
        AlarmSeverity.MAJOR: 2,
        AlarmSeverity.INVALID: 3,
    }


# --- batch reads ------------------------------------------------------------ #


async def test_get_many_reads_and_normalizes_failures():
    conn = FakeConnector(values={"A": _pv("A", 1.0), "B": _pv("B", 2.0)}, fail={"C"})
    out = await EpicsIO(conn, concurrency=4, chunk_size=2).get_many(["A", "B", "C"])
    assert out["A"].value == 1.0 and out["B"].value == 2.0
    assert out["C"].connected is False and "read boom" in out["C"].error  # failure never breaks the batch


async def test_get_many_empty_is_empty():
    assert await EpicsIO(FakeConnector()).get_many([]) == {}


async def test_get_many_progress_is_chunked():
    conn = FakeConnector(values={f"P{i}": _pv(f"P{i}", i) for i in range(5)})
    calls: list[tuple[int, int]] = []

    async def cb(cur: int, total: int, _msg: str) -> None:
        calls.append((cur, total))

    await EpicsIO(conn, chunk_size=2).get_many_with_progress([f"P{i}" for i in range(5)], cb)
    assert calls == [(2, 5), (4, 5), (5, 5)]  # chunks of 2 over 5 PVs


# --- batch writes ----------------------------------------------------------- #


async def test_put_many_reports_ok_and_failures():
    conn = FakeConnector(fail={"BAD"})
    out = await EpicsIO(conn, chunk_size=10).put_many({"X": 1, "Y": 2, "BAD": 3})
    assert out["X"] == (True, None)
    assert out["Y"] == (True, None)
    assert out["BAD"][0] is False and "write boom" in out["BAD"][1]
    assert ("X", 1) in conn.puts and ("Y", 2) in conn.puts
    assert ("BAD", 3) not in conn.puts


async def test_put_many_empty_is_empty():
    assert await EpicsIO(FakeConnector()).put_many({}) == {}
