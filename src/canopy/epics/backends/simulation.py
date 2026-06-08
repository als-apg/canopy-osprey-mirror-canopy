"""Simulation connector backend for testing and easy development.

Default backend so the whole live-PV path runs and tests anywhere. Every PV is
a slow sine wave whose phase is a stable function of the PV name, so each PV has
its own deterministic-per-process waveform. Severity thresholds let the
alarm-styling path be exercised end to end.
"""

import math
import time
import zlib
import asyncio
from dataclasses import dataclass

from canopy.epics.base import Subscription, EventCallback
from canopy.contracts.pv import PVValue
from canopy.contracts.sample import AlarmSeverity


class SimulationConnector:
    backend_name = "simulation"

    def __init__(self, update_hz: float = 5.0, units: str | None = None) -> None:
        self._interval = 1.0 / (update_hz or 5.0)
        self._units = units or None
        self._tasks: set[asyncio.Task] = set()

    @classmethod
    def from_config(cls, cfg) -> "SimulationConnector":
        return cls(update_hz=cfg.sim_update_hz, units=cfg.sim_default_units or None)

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

    def _value_for(self, pv: str) -> PVValue:
        phase = (zlib.crc32(pv.encode()) % 1000) / 1000.0 * 2 * math.pi
        v = round(10.0 * math.sin(2 * math.pi * 0.1 * time.monotonic() + phase), 4)
        mag = abs(v)
        if mag > 9.0:
            severity = AlarmSeverity.MAJOR
        elif mag > 7.0:
            severity = AlarmSeverity.MINOR
        else:
            severity = AlarmSeverity.NO_ALARM
        return PVValue(
            pv=pv,
            value=v,
            severity=severity,
            ts_ns=time.time_ns(),
            connected=True,
            units=self._units,
        )

    async def get(self, pv: str, *, timeout: float | None = None) -> PVValue:
        return self._value_for(pv)

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
