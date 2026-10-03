"""Data-source abstraction for Fox.

Fox never imports Vole directly. It depends only on this ``DataSource`` Protocol
and the callables declared here. Vole's adapter implements ``DataSource`` and
the sample-record port; ``fox_lifespan()`` injects both into ``FormulaService``
and ``SubscriptionManager``.
"""

from typing import TYPE_CHECKING, Any, Protocol, NamedTuple, runtime_checkable
from dataclasses import dataclass
from collections.abc import Callable, Awaitable

import numpy as np

from canopy.contracts.sample import Sample, AlarmSeverity
from canopy.contracts.formula import IndexMode, AlignmentMode

if TYPE_CHECKING:
    from canopy.mole.adapters.base import ArchiveBlock


@dataclass(frozen=True)
class AlignedWindow:
    """Time-aligned arrays returned by ``DataSource.read_aligned``.

    All arrays share the same time index (``ts_ns``). ``arrays`` maps
    variable names to value arrays; ``severities`` maps to ordinal-encoded
    ``AlarmSeverity`` arrays.
    """

    ts_ns: np.ndarray
    arrays: dict[str, np.ndarray]
    severities: dict[str, np.ndarray]


class SampleRecord(NamedTuple):
    """One stored sample as the table evaluator sees it.

    ``value`` is raw (non-numeric → null is the cell fill's job); ``severity``
    lets INVALID → null be decided at the cell; ``in_outage`` says the PV was in
    a recorded outage at the looked-up instant; ``recv_ns`` is the server receive
    time (0 for archive-seeded samples); ``after_connect`` marks the first sample
    after a (re)connect.
    """

    value: Any
    severity: AlarmSeverity
    in_outage: bool
    ts_ns: int
    recv_ns: int
    after_connect: bool


@dataclass(frozen=True)
class RingSlice:
    """One synchronous copy of a ring's samples from a timestamp on, plus its outages.

    ``severities`` are ``AlarmSeverity`` ordinals (``list(AlarmSeverity).index``);
    ``outages`` are ``(start_ns, end_ns)`` on the receive clock, ``end_ns`` None while open.
    """

    ts_ns: np.ndarray  # int64
    values: np.ndarray  # object
    severities: np.ndarray  # int8
    outages: list[tuple[int, int | None]]


# Callables Vole provides and fox_lifespan injects into SubscriptionManager.
PinFn = Callable[[list[str], str], None]
UnpinFn = Callable[[str], Awaitable[list[str]]]
WatermarkFn = Callable[[], int]
EnsureCapacityFn = Callable[..., None]
LastAtOrBeforeFn = Callable[[str, int], SampleRecord | None]
SamplesSinceFn = Callable[[str, int], list[SampleRecord]]
RingSliceFn = Callable[[str, int], RingSlice]
FetchWindowBudgetedFn = Callable[..., Awaitable["ArchiveBlock"]]  # (pv, t0_ns, t1_ns, *, budget=None)
FetchLastBeforeFn = Callable[..., Awaitable[list[Sample | None]]]  # (pv, ts_list)
EventFaithfulFn = Callable[..., Awaitable[bool]]


@runtime_checkable
class DataSource(Protocol):
    """Contract Fox requires from its data provider (Vole, eventually)."""

    async def read_aligned(
        self,
        pvs: list[str],
        t0_ns: int,
        t1_ns: int,
        *,
        align: AlignmentMode,
        index: IndexMode,
        grid_dt_ns: int | None = None,
    ) -> AlignedWindow: ...

    async def read_binned(
        self,
        pvs: list[str],
        t0_ns: int,
        t1_ns: int,
        *,
        target_buckets: int | None = None,
    ) -> AlignedWindow: ...

    async def read_latest(self, pv: str) -> Sample | None: ...


class NullDataSource:
    """Placeholder used until Vole ships. Always signals inputs unavailable."""

    async def read_aligned(
        self,
        pvs: list[str],
        t0_ns: int,
        t1_ns: int,
        *,
        align: AlignmentMode,
        index: IndexMode,
        grid_dt_ns: int | None = None,
    ) -> AlignedWindow:
        from canopy.fox.errors import FormulaInputMissing

        raise FormulaInputMissing(f"Data source not available; PVs requested: {pvs}")

    async def read_binned(
        self,
        pvs: list[str],
        t0_ns: int,
        t1_ns: int,
        *,
        target_buckets: int | None = None,
    ) -> AlignedWindow:
        from canopy.fox.errors import FormulaInputMissing

        raise FormulaInputMissing(f"Data source not available; PVs requested: {pvs}")

    async def read_latest(self, pv: str) -> Sample | None:
        from canopy.fox.errors import FormulaInputMissing

        raise FormulaInputMissing(f"Data source not available; PV requested: {pv}")
