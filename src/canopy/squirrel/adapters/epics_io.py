"""Squirrel's EPICS I/O adapter.

Bridges the squirrel domain (batch-native and metadata-rich — it speaks
``get_many``/``put_many`` returning ``EpicsValue`` / ``(ok, err)``) to Canopy's
per-PV ``canopy.epics`` connector (``get() -> PVValue`` / ``put(pv, value)``).

Why this layer exists: ``snapshot_service`` was written against a batch EPICS
service. Rather than rewrite it, we rebuild that batch surface over the connector —
fanning out per-PV ``get``/``put`` with bounded concurrency, reporting chunked
progress, and re-normalizing per-PV exceptions into the dict contracts the
create/restore code consumes (a read failure never breaks the batch).

A single ``PVValue`` read carries value/severity/timestamp/connected only (units,
precision, status and control limits live in ``PVMeta`` via ``describe()``). v1
snapshots capture the former and leave the latter ``None``; since they live *inside*
the JSONB value blob, enriching later needs no migration.
"""

from __future__ import annotations

import asyncio
from typing import Any
from datetime import UTC, datetime
from dataclasses import dataclass
from collections.abc import Callable, Awaitable

from canopy.epics.base import PVConnector
from canopy.contracts.pv import PVValue
from canopy.contracts.sample import AlarmSeverity

# An async progress sink: ``await cb(current, total, message)``.
ProgressCallback = Callable[[int, int, str], Awaitable[None]]

# Inverse of ``canopy.epics.base._SEVERITY_MAP`` — squirrel persists EPICS severity
# as the integer code (0..3) the original backend stored, not the enum name.
_SEVERITY_TO_INT: dict[AlarmSeverity, int] = {
    AlarmSeverity.NO_ALARM: 0,
    AlarmSeverity.MINOR: 1,
    AlarmSeverity.MAJOR: 2,
    AlarmSeverity.INVALID: 3,
}


@dataclass
class EpicsValue:
    """A single PV read, in the shape the snapshot domain consumes.

    Ported from ``react-squirrel-backend``'s ``epics_types.EpicsValue``. ``status``,
    ``units``, ``precision`` and the control limits are part of the contract but stay
    ``None`` in v1 (a single ``get()`` doesn't carry them — a ``describe()`` pass
    would). ``timestamp`` is a timezone-aware ``datetime`` (never a float), avoiding
    the source's latent ``.isoformat()``-on-float bug on the CA path.
    """

    value: Any
    status: int | None = None
    severity: int | None = None
    timestamp: datetime | None = None
    units: str | None = None
    precision: int | None = None
    upper_ctrl_limit: float | None = None
    lower_ctrl_limit: float | None = None
    connected: bool = True
    error: str | None = None


def _ts_ns_to_datetime(ts_ns: int | None) -> datetime | None:
    if not ts_ns:
        return None
    return datetime.fromtimestamp(ts_ns / 1_000_000_000, tz=UTC)


def _to_epics_value(pv: PVValue) -> EpicsValue:
    """Map a connector ``PVValue`` onto the domain's ``EpicsValue`` shape."""
    return EpicsValue(
        value=pv.value,
        severity=_SEVERITY_TO_INT.get(pv.severity),
        timestamp=_ts_ns_to_datetime(pv.ts_ns),
        connected=pv.connected,
        error=pv.error,
    )


class EpicsIO:
    """Batch EPICS reads/writes over a ``canopy.epics`` connector.

    Concurrency is bounded by a semaphore; progress is reported on a chunk cadence.
    The connector is shared and already started (typically the one the worker builds
    at startup and stashes in the Arq context).
    """

    def __init__(
        self,
        connector: PVConnector,
        *,
        concurrency: int = 64,
        chunk_size: int = 500,
        timeout: float | None = None,
    ) -> None:
        self._connector = connector
        self._concurrency = max(1, concurrency)
        self._chunk_size = max(1, chunk_size)
        self._timeout = timeout

    # --- reads ----------------------------------------------------------- #

    async def get_many(self, pv_names: list[str]) -> dict[str, EpicsValue]:
        return await self.get_many_with_progress(pv_names, None)

    async def get_many_with_progress(
        self, pv_names: list[str], progress_callback: ProgressCallback | None = None
    ) -> dict[str, EpicsValue]:
        results: dict[str, EpicsValue] = {}
        total = len(pv_names)
        if total == 0:
            return results
        sem = asyncio.Semaphore(self._concurrency)
        done = 0
        for start in range(0, total, self._chunk_size):
            chunk = pv_names[start : start + self._chunk_size]
            pairs = await asyncio.gather(*(self._read_one(sem, addr) for addr in chunk))
            results.update(dict(pairs))
            done += len(chunk)
            if progress_callback:
                await progress_callback(done, total, f"Read {done}/{total} PVs...")
        return results

    async def _read_one(self, sem: asyncio.Semaphore, addr: str) -> tuple[str, EpicsValue]:
        async with sem:
            try:
                pv = await self._connector.get(addr, timeout=self._timeout)
                return addr, _to_epics_value(pv)
            except Exception as exc:  # normalize any per-PV failure; never break the batch
                return addr, EpicsValue(value=None, connected=False, error=str(exc))

    # --- writes ---------------------------------------------------------- #

    async def put_many(self, values: dict[str, Any]) -> dict[str, tuple[bool, str | None]]:
        return await self.put_many_with_progress(values, None)

    async def put_many_with_progress(
        self, values: dict[str, Any], progress_callback: ProgressCallback | None = None
    ) -> dict[str, tuple[bool, str | None]]:
        results: dict[str, tuple[bool, str | None]] = {}
        items = list(values.items())
        total = len(items)
        if total == 0:
            return results
        sem = asyncio.Semaphore(self._concurrency)
        done = 0
        for start in range(0, total, self._chunk_size):
            chunk = items[start : start + self._chunk_size]
            pairs = await asyncio.gather(*(self._write_one(sem, addr, value) for addr, value in chunk))
            results.update(dict(pairs))
            done += len(chunk)
            if progress_callback:
                await progress_callback(done, total, f"Wrote {done}/{total} PVs...")
        return results

    async def _write_one(self, sem: asyncio.Semaphore, addr: str, value: Any) -> tuple[str, tuple[bool, str | None]]:
        async with sem:
            try:
                await self._connector.put(addr, value, timeout=self._timeout)
                return addr, (True, None)
            except Exception as exc:  # normalize any per-PV failure; never break the batch
                return addr, (False, str(exc))
