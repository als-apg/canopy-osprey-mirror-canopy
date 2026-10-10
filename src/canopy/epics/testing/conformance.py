"""Conformance suite for `PVConnector` backends.

One set of cases that every backend -- the two shipped here and any out-of-tree
``module:Class`` backend -- runs against itself, so a screen behaves the same
whichever backend serves it. A backend supplies two things:

- a **factory**: a zero-argument callable returning a fresh, unstarted connector;
- a **driver**: the test's handle on the control system behind the connector. It
  names the PVs the suite uses and changes their values from the server side.

The suite covers three value kinds (scalar, enum, waveform) and a PV that never
connects, each under the three name forms a client may send (bare, ``ca://`` and
``pva://``), plus an optional outage case for drivers that can take their control
system down and bring it back.

The module does not import pytest, so it can also run in a helper process. The
usual pytest wiring is::

    from canopy.epics.testing import conformance

    @pytest.mark.parametrize("case_id", conformance.case_ids())
    async def test_conformance(case_id, driver):
        try:
            await conformance.run_case(case_id, make_connector, driver)
        except conformance.CaseSkipped as skip:
            pytest.skip(str(skip))

`run_all` runs every case and returns the outcomes as plain data instead.
"""

import math
import asyncio
import traceback
from typing import Protocol
from dataclasses import dataclass
from collections.abc import Callable, Awaitable

from canopy.epics.base import PVConnector
from canopy.contracts.pv import PVValue, ValueKind, PVWriteValue

# The value kinds every backend serves, and the name forms a client may use.
KINDS: tuple[str, ...] = ("scalar", "enum", "waveform")
FORMS: tuple[str, ...] = ("bare", "ca", "pva")
# The fourth PV a driver names: one that no server answers for.
NEVER = "never"

# A served value is visible to a reader within this long.
VALUE_TIMEOUT_S = 5.0
# A PV that never connects is reported not connected within this long ...
NEVER_REPORT_S = 2.0
# ... and the monitor is watched this long in total, to see that nothing follows.
NEVER_OBSERVE_S = 2.5
# After an outage, the drop is reported and the values are back within these.
DROP_REPORT_S = 2.0
RESTORE_TIMEOUT_S = 10.0

_POLL_S = 0.02

_EXPECTED_KIND = {
    "scalar": ValueKind.SCALAR,
    "enum": ValueKind.ENUM,
    "waveform": ValueKind.WAVEFORM,
}

# Two distinct values per kind: the first is in place before the connector looks,
# the second is set while it is watching. An enum value is the choice index, so a
# driver's enum PV has at least three choices; its waveform holds three elements.
_VALUES: dict[str, tuple[PVWriteValue, PVWriteValue]] = {
    "scalar": (1.25, 2.5),
    "enum": (1, 2),
    "waveform": ([1.0, 2.0, 3.0], [4.0, 5.0, 6.0]),
}


class ConformanceDriver(Protocol):
    """What a backend's test supplies besides the connector factory.

    A driver may also define ``async def drop()`` and ``async def restore()``,
    which make every served PV unreachable and reachable again; without both, the
    outage cases are skipped.
    """

    def pv(self, kind: str, form: str) -> str:
        """The PV name the suite uses for `kind` (one of `KINDS`, or `NEVER`)
        in name form `form` (one of `FORMS`): bare, or carrying the ``ca://`` or
        ``pva://`` prefix. The `NEVER` name must be one nothing serves."""
        ...

    async def set_value(self, pv: str, value: PVWriteValue) -> None:
        """Set `pv` (a name this driver returned from `pv`) to `value` in the
        control system itself, not through the connector under test."""
        ...


ConnectorFactory = Callable[[], PVConnector]


class CaseSkipped(Exception):
    """Raised by a case the driver cannot exercise (an optional capability)."""


@dataclass(frozen=True)
class Case:
    id: str
    run: Callable[[ConnectorFactory, ConformanceDriver], Awaitable[None]]


# --- helpers ----------------------------------------------------------------- #


def _same(actual, expected) -> bool:
    """Value equality with float tolerance, element-wise for waveforms."""
    if isinstance(expected, list):
        return (
            isinstance(actual, list)
            and len(actual) == len(expected)
            and all(_same(a, e) for a, e in zip(actual, expected, strict=True))
        )
    if isinstance(expected, bool) or isinstance(actual, bool):
        return actual is expected
    if isinstance(expected, int | float):
        return isinstance(actual, int | float) and math.isclose(actual, expected, rel_tol=1e-9, abs_tol=1e-9)
    return actual == expected


async def _wait_for(predicate: Callable[[], bool], timeout: float) -> bool:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        if predicate():
            return True
        if loop.time() >= deadline:
            return False
        await asyncio.sleep(_POLL_S)


def _check_events(events: list[PVValue], pv: str, kind: str) -> None:
    """What holds for every event of a monitor on a served PV."""
    for event in events:
        assert event.pv == pv, f"event names {event.pv!r}, the monitor was opened on {pv!r}"
        if event.connected:
            assert event.value_kind is _EXPECTED_KIND[kind], (
                f"{pv}: a {kind} PV was delivered as {event.value_kind.value}"
            )
        else:
            assert event.value is None, f"{pv}: a not-connected event carries the value {event.value!r}"


def _has_value(events: list[PVValue], value) -> bool:
    return any(e.connected and _same(e.value, value) for e in events)


# --- cases ------------------------------------------------------------------- #


async def _case_get(factory: ConnectorFactory, driver: ConformanceDriver, kind: str, form: str) -> None:
    """A one-shot read returns the served value, classified by its kind."""
    pv = driver.pv(kind, form)
    first, _ = _VALUES[kind]
    await driver.set_value(pv, first)
    connector = factory()
    await connector.start()
    try:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + VALUE_TIMEOUT_S
        while True:
            value = await connector.get(pv)
            if value.connected and _same(value.value, first):
                break
            assert loop.time() < deadline, f"get({pv!r}) never returned {first!r}; last answer: {value!r}"
            await asyncio.sleep(_POLL_S)
        assert value.pv == pv, f"get({pv!r}) answered for {value.pv!r}"
        assert value.value_kind is _EXPECTED_KIND[kind], f"{pv}: a {kind} PV was read as {value.value_kind.value}"
    finally:
        await connector.close()


async def _case_monitor(factory: ConnectorFactory, driver: ConformanceDriver, kind: str, form: str) -> None:
    """A monitor delivers the current value, then a value set while it watches."""
    pv = driver.pv(kind, form)
    first, second = _VALUES[kind]
    await driver.set_value(pv, first)
    connector = factory()
    await connector.start()
    events: list[PVValue] = []
    try:
        subscription = await connector.subscribe(pv, events.append)
        try:
            assert await _wait_for(lambda: _has_value(events, first), VALUE_TIMEOUT_S), (
                f"monitor on {pv!r} never delivered the current value {first!r}; got {events!r}"
            )
            await driver.set_value(pv, second)
            assert await _wait_for(lambda: _has_value(events, second), VALUE_TIMEOUT_S), (
                f"monitor on {pv!r} never delivered the new value {second!r}; got {events!r}"
            )
        finally:
            await subscription.close()
        _check_events(events, pv, kind)
    finally:
        await connector.close()


async def _case_never(factory: ConnectorFactory, driver: ConformanceDriver, form: str) -> None:
    """A PV nothing serves is reported not connected once, and nothing else."""
    pv = driver.pv(NEVER, form)
    connector = factory()
    await connector.start()
    events: list[PVValue] = []
    try:
        loop = asyncio.get_running_loop()
        opened = loop.time()
        subscription = await connector.subscribe(pv, events.append)
        try:
            assert await _wait_for(lambda: bool(events), NEVER_REPORT_S), (
                f"monitor on the never-connecting {pv!r} reported nothing within {NEVER_REPORT_S} s"
            )
            await asyncio.sleep(max(0.0, opened + NEVER_OBSERVE_S - loop.time()))
        finally:
            await subscription.close()
        assert len(events) == 1, f"never-connecting {pv!r} produced {len(events)} events, expected one: {events!r}"
        event = events[0]
        assert event.connected is False, f"never-connecting {pv!r} was reported connected: {event!r}"
        assert event.value is None, f"never-connecting {pv!r} carries the value {event.value!r}"
        assert event.pv == pv, f"event names {event.pv!r}, the monitor was opened on {pv!r}"
    finally:
        await connector.close()


async def _case_drop_restore(factory: ConnectorFactory, driver: ConformanceDriver, form: str) -> None:
    """An outage is reported as not connected; values return when it ends."""
    drop = getattr(driver, "drop", None)
    restore = getattr(driver, "restore", None)
    if drop is None or restore is None:
        raise CaseSkipped("the driver cannot drop and restore its control system")
    pv = driver.pv("scalar", form)
    first, second = _VALUES["scalar"]
    await driver.set_value(pv, first)
    connector = factory()
    await connector.start()
    events: list[PVValue] = []
    try:
        subscription = await connector.subscribe(pv, events.append)
        try:
            assert await _wait_for(lambda: _has_value(events, first), VALUE_TIMEOUT_S), (
                f"monitor on {pv!r} never delivered the current value {first!r}; got {events!r}"
            )
            before = len(events)
            dropped = False
            try:
                await drop()
                dropped = True
                assert await _wait_for(lambda: any(not e.connected for e in events[before:]), DROP_REPORT_S), (
                    f"monitor on {pv!r} did not report the outage within {DROP_REPORT_S} s; got {events[before:]!r}"
                )
            finally:
                if dropped:
                    await restore()
            await driver.set_value(pv, second)
            assert await _wait_for(lambda: _has_value(events, second), RESTORE_TIMEOUT_S), (
                f"monitor on {pv!r} did not recover within {RESTORE_TIMEOUT_S} s; got {events[before:]!r}"
            )
        finally:
            await subscription.close()
        _check_events(events, pv, "scalar")
    finally:
        await connector.close()


def _bind(fn, *args) -> Callable[[ConnectorFactory, ConformanceDriver], Awaitable[None]]:
    async def run(factory: ConnectorFactory, driver: ConformanceDriver) -> None:
        await fn(factory, driver, *args)

    return run


def _build_cases() -> tuple[Case, ...]:
    cases: list[Case] = []
    for kind in KINDS:
        for form in FORMS:
            cases.append(Case(f"get-{kind}-{form}", _bind(_case_get, kind, form)))
            cases.append(Case(f"monitor-{kind}-{form}", _bind(_case_monitor, kind, form)))
    for form in FORMS:
        cases.append(Case(f"never-connecting-{form}", _bind(_case_never, form)))
    for form in FORMS:
        cases.append(Case(f"drop-restore-{form}", _bind(_case_drop_restore, form)))
    return tuple(cases)


CASES: tuple[Case, ...] = _build_cases()
_BY_ID = {case.id: case for case in CASES}


def case_ids() -> list[str]:
    """The id of every case, in run order -- the parametrisation of a test."""
    return [case.id for case in CASES]


async def run_case(case_id: str, factory: ConnectorFactory, driver: ConformanceDriver) -> None:
    """Run one case. Raises `AssertionError` when the backend does not conform
    and `CaseSkipped` when the driver lacks the capability the case needs."""
    await _BY_ID[case_id].run(factory, driver)


async def run_all(factory: ConnectorFactory, driver: ConformanceDriver) -> dict[str, dict[str, str]]:
    """Run every case; return ``{case id: {"outcome": ..., "detail": ...}}`` with
    outcome ``passed``, ``failed`` or ``skipped``. Never raises for a failing case."""
    results: dict[str, dict[str, str]] = {}
    for case in CASES:
        try:
            await case.run(factory, driver)
        except CaseSkipped as skip:
            results[case.id] = {"outcome": "skipped", "detail": str(skip)}
        except Exception:
            results[case.id] = {"outcome": "failed", "detail": traceback.format_exc()}
        else:
            results[case.id] = {"outcome": "passed", "detail": ""}
    return results
