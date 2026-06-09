"""Public surface of the Vole module for in-process callers.

Other Canopy modules import only from this file; the rest of ``canopy.vole.*``
is private implementation.
"""

from __future__ import annotations

from canopy.contracts.sample import Sample
from canopy.vole.schemas.buffer import VoleStatsDTO
from canopy.vole.services.hare_ingest import HareIngestService
from canopy.vole.services.buffer_service import BufferService
from canopy.vole.services.alignment_service import AlignedResult

_service: BufferService | None = None
_ingest: HareIngestService | None = None


def bind(service: BufferService | None) -> None:
    global _service
    _service = service


def bind_ingest(ingest: HareIngestService | None) -> None:
    global _ingest
    _ingest = ingest


def get_service() -> BufferService:
    if _service is None:
        raise RuntimeError("Vole gateway accessed before app startup bound a service")
    return _service


# --- writes -------------------------------------------------------------- #


async def append(pv: str, sample: Sample) -> None:
    await get_service().append(pv, sample)


async def seed(
    pv: str,
    samples: list[Sample],
    *,
    max_age_ns: int | None = None,
    max_points: int | None = None,
) -> int:
    return await get_service().seed(pv, samples, max_age_ns=max_age_ns, max_points=max_points)


async def drop(pv: str) -> None:
    await get_service().drop(pv)


# --- reads --------------------------------------------------------------- #


async def read_window(
    pv: str,
    t0_ns: int,
    t1_ns: int,
    *,
    align: str = "last",
    max_points: int | None = None,
) -> list[Sample]:
    return await get_service().read_window(pv, t0_ns, t1_ns, align=align, max_points=max_points)


async def read_aligned(
    pvs: list[str],
    t0_ns: int,
    t1_ns: int,
    *,
    align: str = "last",
    index: str = "union",
    grid_dt_ns: int | None = None,
) -> AlignedResult:
    return await get_service().read_aligned(pvs, t0_ns, t1_ns, align=align, index=index, grid_dt_ns=grid_dt_ns)


async def read_latest(pv: str) -> Sample | None:
    return await get_service().read_latest(pv)


# --- introspection ------------------------------------------------------- #


async def stats() -> VoleStatsDTO:
    return await get_service().stats()


# --- live subscription (Hare integration) -------------------------------- #


async def subscribe_live(pvs: list[str]) -> None:
    """Subscribe Vole to live updates from Hare for these PVs."""
    if _ingest is None:
        raise RuntimeError("Vole gateway accessed before app startup bound ingest")
    await _ingest.subscribe(pvs)


async def unsubscribe_live(pvs: list[str]) -> None:
    """Unsubscribe from live updates for these PVs."""
    if _ingest is None:
        raise RuntimeError("Vole gateway accessed before app startup bound ingest")
    await _ingest.unsubscribe(pvs)


# --- deferred (live tailing not implemented) ----------------------------- #


async def subscribe_window(pv: str, lookback_ns: int):  # noqa: ANN201
    raise NotImplementedError("subscribe_window requires Wren (not yet implemented)")


async def register_derived_ring(
    name: str,
    inputs: list[str],
    on_input_step,  # noqa: ANN001
    *,
    max_age_ns: int | None = None,
    max_points: int | None = None,
) -> None:
    raise NotImplementedError("Derived rings require Wren (not yet implemented)")


async def unregister_derived_ring(name: str) -> None:
    raise NotImplementedError("Derived rings require Wren (not yet implemented)")
