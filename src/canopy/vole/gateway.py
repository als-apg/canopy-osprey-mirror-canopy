"""Public surface of the Vole module for in-process callers.

Other Canopy modules import only from this file; the rest of ``canopy.vole.*``
is private implementation.
"""

from __future__ import annotations

from collections.abc import Callable, Awaitable

from canopy.contracts.sample import Sample
from canopy.vole.schemas.buffer import VoleStatsDTO
from canopy.vole.services.notifier import ChangeNotifier
from canopy.vole.services.hare_ingest import HareIngestService
from canopy.vole.services.buffer_service import BufferService
from canopy.vole.services.alignment_service import AlignedResult

_service: BufferService | None = None
_ingest: HareIngestService | None = None
_notifier: ChangeNotifier | None = None


def bind(service: BufferService | None) -> None:
    global _service
    _service = service


def bind_ingest(ingest: HareIngestService | None) -> None:
    global _ingest
    _ingest = ingest


def bind_notifier(notifier: ChangeNotifier | None) -> None:
    global _notifier
    _notifier = notifier


def get_notifier() -> ChangeNotifier:
    if _notifier is None:
        raise RuntimeError("Vole notifier accessed before app startup bound it")
    return _notifier


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


# --- change notifications ------------------------------------------------ #


def on_change(pv: str, callback: Callable[[str], Awaitable[None]]) -> Callable[[], None]:
    """Subscribe to notifications when a PV ring receives new data.

    Returns an unsubscribe function. Used by Fox to trigger re-evaluation.
    """
    return get_notifier().subscribe(pv, callback)
