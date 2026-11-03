"""Shared rig of the live-table tests: Vole's real ring behind the table port, a scripted archive, hand-driven clocks."""

from __future__ import annotations

import json
import asyncio

from canopy.core.clock import FakeClock
from canopy.vole.config import VoleConfig
from canopy.vole.adapter import VoleDataSource
from canopy.contracts.sample import Sample, AlarmSeverity
from canopy.contracts.formula import FormulaSpec
from canopy.vole.buffer.store import PVStore
from canopy.mole.adapters.base import ArchiveBlock
from canopy.vole.services.buffer_service import BufferService
from canopy.vole.services.eviction_service import EvictionService
from canopy.vole.services.alignment_service import AlignmentService
from canopy.formula_engine.services.table_store import TablePort

S = 1_000_000_000
MS = 1_000_000
T0 = 1_800_000_000 * S  # "now" at subscribe time, on both clocks
HOUR = 3600 * S


class Harness:
    """Vole rings behind the real port, a scripted archive, and hand-driven clocks."""

    def __init__(self) -> None:
        config = VoleConfig(max_points=200_000, global_bytes_max=2_000_000_000)
        self.clock = FakeClock(start_ns=T0)
        store = PVStore(config)
        self.buffer = BufferService(store, AlignmentService(), EvictionService(store, config), self.clock, config)
        self.source = VoleDataSource(self.buffer)
        self.now = T0
        self.wm = 0
        self.archive: dict[str, list[tuple[int, object, AlarmSeverity]]] = {}
        self.errors: dict[str, Exception] = {}
        self.calls: list[tuple[str, str, int, int]] = []
        self.faithful = True
        self.gate: asyncio.Event | None = None
        self.archiver = True  # False: no Mole configured

    # --- clocks and live data ---------------------------------------------- #

    def advance(self, ns: int, *, wm: bool = True) -> None:
        self.now += ns
        if wm:
            self.wm = self.now

    async def live(
        self,
        pv: str,
        ts: int,
        value: object = 1.0,
        *,
        recv: int | None = None,
        severity: AlarmSeverity = AlarmSeverity.NO_ALARM,
        after_connect: bool = False,
    ) -> None:
        # model_construct: Hare also hands Vole waveform (list) and table (dict) values.
        sample = Sample.model_construct(ts_ns=ts, value=value, severity=severity, source="live")
        await self.buffer.append(pv, sample, recv_ns=self.now if recv is None else recv, after_connect=after_connect)

    def archived(self, pv: str, points: list[tuple[int, object]] | list[tuple[int, object, AlarmSeverity]]) -> None:
        rows = []
        for p in points:
            rows.append((p[0], p[1], p[2] if len(p) > 2 else AlarmSeverity.NO_ALARM))
        self.archive[pv] = sorted(rows)

    # --- archive fakes ------------------------------------------------------ #

    def mole_calls(self, pv: str | None = None) -> list[tuple[str, str, int, int]]:
        return [c for c in self.calls if pv is None or c[1] == pv]

    async def fetch_window_budgeted(self, pv: str, t0: int, t1: int, *, budget: int | None = None) -> ArchiveBlock:
        self.calls.append(("window", pv, t0, t1))
        if self.gate is not None:
            await self.gate.wait()
        if pv in self.errors:
            raise self.errors[pv]
        rows = self.archive.get(pv, [])
        inside = [r for r in rows if t0 <= r[0] <= t1]
        prior = [r for r in rows if r[0] < t0]
        if prior:
            inside = [prior[-1], *inside]
        return ArchiveBlock.from_samples([Sample(ts_ns=t, value=v, severity=s, source="archive") for t, v, s in inside])

    async def fetch_last_before(self, pv: str, ts_list: list[int]) -> list[Sample | None]:
        assert len(ts_list) <= 500
        self.calls.append(("last_before", pv, min(ts_list, default=0), max(ts_list, default=0)))
        if pv in self.errors:
            raise self.errors[pv]
        rows = self.archive.get(pv, [])
        out = []
        for t in ts_list:
            prior = [r for r in rows if r[0] <= t]
            out.append(
                Sample(ts_ns=prior[-1][0], value=prior[-1][1], severity=prior[-1][2], source="archive")
                if prior
                else None
            )
        return out

    async def event_faithful(self) -> bool:
        return self.faithful

    def port(self, **extra) -> TablePort:
        """The table port over this harness; ``extra`` adds the pin and capacity calls."""
        return TablePort(
            last_at_or_before=self.source.last_at_or_before,
            samples_since=self.source.samples_since,
            ring_slice=self.source.ring_slice,
            now_ns=lambda: self.now,
            watermark=lambda: self.wm,
            fetch_window_budgeted=self.fetch_window_budgeted if self.archiver else None,
            fetch_last_before=self.fetch_last_before if self.archiver else None,
            event_faithful=self.event_faithful,
            **extra,
        )


def trigger_spec(n: int = 1000, *, y: dict[str, str] | None = None, trig: str = "TRIG", **cols) -> FormulaSpec:
    ys = y if y is not None else {"y1": "Y1"}
    bindings = {"x": "X", **ys, "trig": trig}
    columns = {"x": "{x}", **{k: "{" + k + "}" for k in ys}, **cols}
    return FormulaSpec(
        kind="table", bindings=bindings, columns=columns, index="first_input", trigger="trig", max_points=n
    )


def timer_spec(n: int = 1000, *, dt_ns: int = S, y: dict[str, str] | None = None) -> FormulaSpec:
    ys = y if y is not None else {"y1": "Y1"}
    bindings = {"x": "X", **ys}
    columns = {"x": "{x}", **{k: "{" + k + "}" for k in ys}}
    return FormulaSpec(kind="table", bindings=bindings, columns=columns, index="grid", grid_dt_ns=dt_ns, max_points=n)


def strict_json(text: str):
    def _reject(token: str):
        raise ValueError(f"non-strict JSON constant {token}")

    return json.loads(text, parse_constant=_reject)
