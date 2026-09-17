"""Simulation connector backend for testing and easy development.

Default backend so the whole live-PV path runs and tests anywhere, with no EPICS
infrastructure. The PV name's suffix selects the value kind so every type can be
exercised end to end:

- ``:ENUM``               -> an enum cycling through fixed choices
- ``:WAVEFORM`` / ``:WF`` -> a numeric waveform (animated sine across the array)
- ``:STR``                -> a string value
- ``:TABLE``              -> a small NTTable-style table (index/value/status columns)
- anything else           -> a scalar

Each PV's phase is a stable function of its name, so values are deterministic per
process. Severity thresholds let the alarm-styling path be exercised too, and every
value carries the EPICS alarm status code that matches its severity.

Writes can be made to fail on demand — for a named set of PVs, or at a configured
rate drawn from a seeded RNG — so a caller's partial-failure handling can be
exercised in the dev stack without an IOC.
"""

import math
import time
import zlib
import random
import asyncio
from dataclasses import dataclass

from canopy.epics.base import Subscription, EventCallback, PVWriteFailed
from canopy.contracts.pv import PVMeta, PVValue, ValueKind, TableValue, PVWriteValue
from canopy.contracts.sample import AlarmSeverity

# Fixed choice set for simulated enum PVs (names ending in ``:ENUM``).
_ENUM_CHOICES = ["OFF", "LOW", "HIGH", "FAULT"]

# EPICS alarm status code reported alongside each simulated severity.
_STATUS_FOR_SEVERITY: dict[AlarmSeverity, int] = {
    AlarmSeverity.NO_ALARM: 0,
    AlarmSeverity.MINOR: 4,
    AlarmSeverity.MAJOR: 3,
    AlarmSeverity.INVALID: 17,
}


def _parse_fail_pvs(raw: str) -> frozenset[str]:
    """Split a comma-separated PV list into a set of names.

    Parameters
    ----------
    raw : str
        Comma-separated PV names; blanks and surrounding whitespace are dropped.

    Returns
    -------
    frozenset[str]
        The named PVs, empty when `raw` names none.
    """
    return frozenset(name.strip() for name in raw.split(",") if name.strip())


class SimulationConnector:
    """In-process `PVConnector` that generates values instead of talking to an IOC.

    Values written through `put` are held in memory and returned by subsequent reads,
    standing in for an IOC's record state. Writes can also be made to fail, so a
    caller's partial-failure handling is reachable without real hardware.
    """

    backend_name = "simulation"

    def __init__(
        self,
        update_hz: float = 5.0,
        units: str | None = None,
        waveform_length: int = 64,
        max_array_elements: int = 100_000,
        put_error_rate: float = 0.0,
        put_fail_pvs: frozenset[str] = frozenset(),
        seed: int | None = None,
    ) -> None:
        """Build a simulation connector.

        Parameters
        ----------
        update_hz : float, optional
            Monitor tick rate, in updates per second.
        units : str | None, optional
            Engineering units reported by `describe` for scalar and waveform PVs.
        waveform_length : int, optional
            Element count of simulated waveform PVs, capped by `max_array_elements`.
        max_array_elements : int, optional
            Ceiling on the length of a generated array.
        put_error_rate : float, optional
            Fraction of writes to fail, in ``[0.0, 1.0]``.
        put_fail_pvs : frozenset[str], optional
            PV names whose writes always fail, whatever `put_error_rate` is.
        seed : int | None, optional
            Seed for this instance's private failure RNG. `None` seeds from the
            system entropy source.
        """
        self._interval = 1.0 / (update_hz or 5.0)
        self._units = units or None
        self._waveform_length = max(1, min(waveform_length, max_array_elements))
        self._tasks: set[asyncio.Task] = set()
        self._overrides: dict[str, PVWriteValue] = {}
        self._put_error_rate = put_error_rate
        self._put_fail_pvs = frozenset(put_fail_pvs)
        self._rng = random.Random(seed)

    @classmethod
    def from_config(cls, cfg) -> "SimulationConnector":
        """Build a connector from an `EpicsConfig`.

        Parameters
        ----------
        cfg : EpicsConfig
            Configuration supplying the `sim_*` value-generation and
            failure-injection settings plus `max_array_elements`.

        Returns
        -------
        SimulationConnector
            A connector wired to that configuration.
        """
        return cls(
            update_hz=cfg.sim_update_hz,
            units=cfg.sim_default_units or None,
            waveform_length=cfg.sim_waveform_length,
            max_array_elements=cfg.max_array_elements,
            put_error_rate=cfg.sim_put_error_rate,
            put_fail_pvs=_parse_fail_pvs(cfg.sim_put_fail_pvs),
            seed=cfg.sim_seed,
        )

    async def start(self) -> None:
        return None

    async def close(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        for task in list(self._tasks):
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._tasks.clear()

    # --- value generation ------------------------------------------------ #

    @staticmethod
    def _kind_for(pv: str) -> ValueKind:
        if pv.endswith(":ENUM"):
            return ValueKind.ENUM
        if pv.endswith((":WAVEFORM", ":WF")):
            return ValueKind.WAVEFORM
        if pv.endswith(":STR"):
            return ValueKind.STRING
        if pv.endswith(":TABLE"):
            return ValueKind.TABLE
        return ValueKind.SCALAR

    @staticmethod
    def _phase(pv: str) -> float:
        return (zlib.crc32(pv.encode()) % 1000) / 1000.0 * 2 * math.pi

    def _sine(self, pv: str) -> float:
        return round(10.0 * math.sin(2 * math.pi * 0.1 * time.monotonic() + self._phase(pv)), 4)

    @staticmethod
    def _severity_for(mag: float) -> AlarmSeverity:
        if mag > 9.0:
            return AlarmSeverity.MAJOR
        if mag > 7.0:
            return AlarmSeverity.MINOR
        return AlarmSeverity.NO_ALARM

    @staticmethod
    def _status_for(severity: AlarmSeverity) -> int:
        """The EPICS alarm status code a simulated severity is reported with.

        Parameters
        ----------
        severity : AlarmSeverity
            Severity of the generated value.

        Returns
        -------
        int
            NO_ALARM (0) for a healthy value, HIGH (4) for MINOR, HIHI (3) for
            MAJOR and UDF (17) for INVALID.
        """
        return _STATUS_FOR_SEVERITY[severity]

    def _value_for(self, pv: str) -> PVValue:
        """Generate the current value of a simulated PV.

        A PV pinned by a prior `put` is returned verbatim, keeping the
        name-derived value kind so a write round-trips through a read.

        Parameters
        ----------
        pv : str
            PV name; its suffix selects the value kind.

        Returns
        -------
        PVValue
            The value, its kind, and an alarm severity with the matching status.
        """
        kind = self._kind_for(pv)
        now = time.time_ns()
        if pv in self._overrides:
            return PVValue(
                pv=pv,
                value=self._overrides[pv],
                value_kind=kind,
                severity=AlarmSeverity.NO_ALARM,
                alarm_status=self._status_for(AlarmSeverity.NO_ALARM),
                ts_ns=now,
            )
        if kind is ValueKind.ENUM:
            idx = int(time.monotonic() / 2) % len(_ENUM_CHOICES)
            severity = AlarmSeverity.MAJOR if _ENUM_CHOICES[idx] == "FAULT" else AlarmSeverity.NO_ALARM
            return PVValue(
                pv=pv,
                value=idx,
                value_kind=kind,
                severity=severity,
                alarm_status=self._status_for(severity),
                ts_ns=now,
            )
        if kind is ValueKind.WAVEFORM:
            t = 2 * math.pi * 0.1 * time.monotonic() + self._phase(pv)
            n = self._waveform_length
            wf = [round(10.0 * math.sin(t + i * 2 * math.pi / n), 4) for i in range(n)]
            return PVValue(
                pv=pv,
                value=wf,
                value_kind=kind,
                severity=AlarmSeverity.NO_ALARM,
                alarm_status=self._status_for(AlarmSeverity.NO_ALARM),
                ts_ns=now,
            )
        if kind is ValueKind.STRING:
            return PVValue(
                pv=pv,
                value=f"sample={self._sine(pv):+.3f}",
                value_kind=kind,
                severity=AlarmSeverity.NO_ALARM,
                alarm_status=self._status_for(AlarmSeverity.NO_ALARM),
                ts_ns=now,
            )
        if kind is ValueKind.TABLE:
            n = 4
            base = self._sine(pv)
            rows = list(range(1, n + 1))
            vals = [round(base + i, 3) for i in range(n)]
            status = [_ENUM_CHOICES[(i + int(time.monotonic())) % len(_ENUM_CHOICES)] for i in range(n)]
            table = TableValue(labels=["Index", "Value", "Status"], columns=[rows, vals, status])
            return PVValue(
                pv=pv,
                value=table,
                value_kind=kind,
                severity=AlarmSeverity.NO_ALARM,
                alarm_status=self._status_for(AlarmSeverity.NO_ALARM),
                ts_ns=now,
            )
        v = self._sine(pv)
        severity = self._severity_for(abs(v))
        return PVValue(
            pv=pv,
            value=v,
            value_kind=kind,
            severity=severity,
            alarm_status=self._status_for(severity),
            ts_ns=now,
        )

    async def get(self, pv: str, *, timeout: float | None = None) -> PVValue:
        return self._value_for(pv)

    async def put(self, pv: str, value: PVWriteValue, *, timeout: float | None = None, wait: bool = True) -> None:
        """Pin a PV's value so subsequent reads and monitor ticks return it.

        The override is never evicted, which is bounded in practice by the small set
        of distinct PVs a dev or test session writes to.

        Parameters
        ----------
        pv : str
            PV name to write.
        value : PVWriteValue
            Value to hold as this PV's current value.
        timeout : float | None, optional
            Accepted for protocol compatibility; the write completes immediately.
        wait : bool, optional
            Accepted for protocol compatibility; the write completes immediately.

        Raises
        ------
        PVWriteFailed
            When `pv` is one of the configured always-failing PVs, or when the
            instance's RNG draws below the configured put error rate.
        """
        if pv in self._put_fail_pvs or self._rng.random() < self._put_error_rate:
            raise PVWriteFailed(f"simulated put failure for {pv}")
        self._overrides[pv] = value

    async def describe(self, pv: str) -> PVMeta:
        kind = self._kind_for(pv)
        if kind is ValueKind.ENUM:
            return PVMeta(pv=pv, value_kind=kind, enum_choices=list(_ENUM_CHOICES), writable=True)
        if kind is ValueKind.WAVEFORM:
            return PVMeta(pv=pv, value_kind=kind, units=self._units, display_low=-10.0, display_high=10.0)
        if kind is ValueKind.STRING:
            return PVMeta(pv=pv, value_kind=kind, writable=True)
        if kind is ValueKind.TABLE:
            return PVMeta(pv=pv, value_kind=kind)
        return PVMeta(
            pv=pv,
            value_kind=kind,
            units=self._units,
            precision=4,
            display_low=-10.0,
            display_high=10.0,
            control_low=-10.0,
            control_high=10.0,
            warning_low=-7.0,
            warning_high=7.0,
            alarm_low=-9.0,
            alarm_high=9.0,
            writable=True,
        )

    # --- monitors -------------------------------------------------------- #

    async def subscribe(self, pv: str, on_event: EventCallback) -> Subscription:
        task = asyncio.create_task(self._run(pv, on_event))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return _SimSubscription(task)

    async def _run(self, pv: str, on_event: EventCallback) -> None:
        # Emit an initial value immediately, then once per tick.
        on_event(self._value_for(pv))
        while True:
            await asyncio.sleep(self._interval)
            on_event(self._value_for(pv))


@dataclass
class _SimSubscription:
    task: asyncio.Task

    async def close(self) -> None:
        self.task.cancel()
        try:
            await self.task
        except asyncio.CancelledError:
            pass
