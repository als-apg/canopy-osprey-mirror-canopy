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
process. Severity thresholds let the alarm-styling path be exercised too.
"""

import math
import time
import zlib
import asyncio
from dataclasses import dataclass

from canopy.epics.base import Subscription, EventCallback
from canopy.contracts.pv import PVMeta, PVValue, ValueKind, TableValue
from canopy.contracts.sample import AlarmSeverity

# Fixed choice set for simulated enum PVs (names ending in ``:ENUM``).
_ENUM_CHOICES = ["OFF", "LOW", "HIGH", "FAULT"]


class SimulationConnector:
    backend_name = "simulation"

    def __init__(
        self,
        update_hz: float = 5.0,
        units: str | None = None,
        waveform_length: int = 64,
        max_array_elements: int = 100_000,
    ) -> None:
        self._interval = 1.0 / (update_hz or 5.0)
        self._units = units or None
        self._waveform_length = max(1, min(waveform_length, max_array_elements))
        self._tasks: set[asyncio.Task] = set()

    @classmethod
    def from_config(cls, cfg) -> "SimulationConnector":
        return cls(
            update_hz=cfg.sim_update_hz,
            units=cfg.sim_default_units or None,
            waveform_length=cfg.sim_waveform_length,
            max_array_elements=cfg.max_array_elements,
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

    def _value_for(self, pv: str) -> PVValue:
        kind = self._kind_for(pv)
        now = time.time_ns()
        if kind is ValueKind.ENUM:
            idx = int(time.monotonic() / 2) % len(_ENUM_CHOICES)
            severity = AlarmSeverity.MAJOR if _ENUM_CHOICES[idx] == "FAULT" else AlarmSeverity.NO_ALARM
            return PVValue(pv=pv, value=idx, value_kind=kind, severity=severity, ts_ns=now)
        if kind is ValueKind.WAVEFORM:
            t = 2 * math.pi * 0.1 * time.monotonic() + self._phase(pv)
            n = self._waveform_length
            wf = [round(10.0 * math.sin(t + i * 2 * math.pi / n), 4) for i in range(n)]
            return PVValue(pv=pv, value=wf, value_kind=kind, severity=AlarmSeverity.NO_ALARM, ts_ns=now)
        if kind is ValueKind.STRING:
            return PVValue(
                pv=pv,
                value=f"sample={self._sine(pv):+.3f}",
                value_kind=kind,
                severity=AlarmSeverity.NO_ALARM,
                ts_ns=now,
            )
        if kind is ValueKind.TABLE:
            n = 4
            base = self._sine(pv)
            rows = list(range(1, n + 1))
            vals = [round(base + i, 3) for i in range(n)]
            status = [_ENUM_CHOICES[(i + int(time.monotonic())) % len(_ENUM_CHOICES)] for i in range(n)]
            table = TableValue(labels=["Index", "Value", "Status"], columns=[rows, vals, status])
            return PVValue(pv=pv, value=table, value_kind=kind, severity=AlarmSeverity.NO_ALARM, ts_ns=now)
        v = self._sine(pv)
        return PVValue(pv=pv, value=v, value_kind=kind, severity=self._severity_for(abs(v)), ts_ns=now)

    async def get(self, pv: str, *, timeout: float | None = None) -> PVValue:
        return self._value_for(pv)

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
