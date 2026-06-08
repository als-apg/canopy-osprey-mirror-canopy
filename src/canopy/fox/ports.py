"""Data-source abstraction for Fox.

Fox never imports Vole directly. It depends only on this ``DataSource`` Protocol.
When Vole ships, its gateway implements ``DataSource`` and is injected into
``FormulaService`` via ``fox_lifespan()``.
"""

from typing import Protocol, runtime_checkable
from dataclasses import dataclass

import numpy as np

from canopy.contracts.sample import Sample
from canopy.contracts.formula import IndexMode, AlignmentMode


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

    async def read_latest(self, pv: str) -> Sample | None:
        from canopy.fox.errors import FormulaInputMissing

        raise FormulaInputMissing(f"Data source not available; PV requested: {pv}")
