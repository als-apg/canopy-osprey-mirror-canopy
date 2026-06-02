"""DataSource adapter: bridges Vole's BufferService to Fox's DataSource protocol.

Fox imports this class; it is constructed in fox_lifespan() and injected
into FormulaService. Fox never imports canopy.vole.gateway directly.
"""

from __future__ import annotations

from canopy.fox.ports import AlignedWindow
from canopy.contracts.sample import Sample
from canopy.contracts.formula import IndexMode, AlignmentMode
from canopy.vole.services.buffer_service import BufferService

_ALIGN_MAP: dict[AlignmentMode, str] = {
    "last": "last",
    "interpolate": "linear",
}

_INDEX_MAP: dict[IndexMode, str] = {
    "union": "union",
    "intersection": "intersection",
    "grid": "grid",
    "first_input": "first_input",
}


class VoleDataSource:
    """Implements Fox's DataSource protocol by delegating to BufferService."""

    def __init__(self, buffer_service: BufferService) -> None:
        self._buffer = buffer_service

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
        vole_align = _ALIGN_MAP[align]
        vole_index = _INDEX_MAP[index]

        result = await self._buffer.read_aligned(
            pvs,
            t0_ns,
            t1_ns,
            align=vole_align,
            index=vole_index,
            grid_dt_ns=grid_dt_ns,
        )

        return AlignedWindow(
            ts_ns=result.ts_ns,
            arrays=result.arrays,
            severities=result.severities,
        )

    async def read_latest(self, pv: str) -> Sample | None:
        return await self._buffer.read_latest(pv)
