"""Public surface of the Vole module for in-process callers.

Other Canopy modules import only from this file; the rest of ``canopy.vole.*``
is private implementation.
"""

from __future__ import annotations

from collections.abc import Callable, Awaitable

from canopy.vole.errors import IngestUnavailable
from canopy.contracts.sample import Sample
from canopy.vole.buffer.ring import Outage, StoredSample
from canopy.epics.pv_protocol import strip_protocol_prefix
from canopy.vole.schemas.buffer import VoleStatsDTO
from canopy.vole.services.notifier import ChangeNotifier
from canopy.vole.services.hare_ingest import HareIngestService
from canopy.vole.services.grace_manager import GraceManager
from canopy.vole.services.buffer_service import BufferService
from canopy.vole.services.alignment_service import AlignedResult

_service: BufferService | None = None
_ingest: HareIngestService | None = None
_notifier: ChangeNotifier | None = None
_grace: GraceManager | None = None


def bind(service: BufferService | None) -> None:
    global _service
    _service = service


def bind_ingest(ingest: HareIngestService | None) -> None:
    global _ingest
    _ingest = ingest


def bind_notifier(notifier: ChangeNotifier | None) -> None:
    global _notifier
    _notifier = notifier


def bind_grace(grace: GraceManager | None) -> None:
    global _grace
    _grace = grace


def get_notifier() -> ChangeNotifier:
    if _notifier is None:
        raise RuntimeError("Vole notifier accessed before app startup bound it")
    return _notifier


def get_service() -> BufferService:
    if _service is None:
        raise RuntimeError("Vole gateway accessed before app startup bound a service")
    return _service


def get_ingest() -> HareIngestService:
    if _ingest is None:
        raise IngestUnavailable("Vole live ingestion is not available (Hare not connected)")
    return _ingest


# --- writes -------------------------------------------------------------- #


async def append(pv: str, sample: Sample) -> None:
    await get_service().append(pv, sample)


async def seed(
    pv: str,
    samples: list[Sample],
    *,
    max_points: int | None = None,
) -> int:
    return await get_service().seed(pv, samples, max_points=max_points)


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


# --- sample-record port (synchronous, per-PV state) ---------------------- #


def last_at_or_before(pv: str, ts_ns: int) -> StoredSample | None:
    return get_service().last_at_or_before(pv, ts_ns)


def in_outage(pv: str, ts_ns: int) -> bool:
    return get_service().in_outage(pv, ts_ns)


def samples_since(pv: str, recv_after_ns: int) -> list[StoredSample]:
    return get_service().samples_since(pv, recv_after_ns)


def ring_slice(pv: str, ts_from_ns: int) -> tuple[list[StoredSample], list[Outage]]:
    return get_service().ring_slice(pv, ts_from_ns)


def watermark_ns() -> int:
    """Receive time of the newest Hare flush ingest has fully applied; 0 without ingest."""
    return _ingest.watermark_ns if _ingest is not None else 0


# --- pins and capacity --------------------------------------------------- #


def pin(pvs: list[str], holder: str) -> None:
    """Keep these PVs' rings and ingest alive for ``holder`` (a live table eval)."""
    get_service().pin(pvs, holder)


async def unpin(holder: str) -> list[str]:
    """Drop every pin of ``holder``; a deactivation expiry skipped while pinned is re-armed."""
    released = get_service().unpin(holder)
    if released and _grace is not None:
        await _grace.on_pvs_unpinned(released)
    return released


def ensure_capacity(pv: str, max_points: int | None = None) -> None:
    """Grow-only: a table input's ring holds at least ``settle_depth`` samples."""
    get_service().ensure_capacity(strip_protocol_prefix(pv), max_points)


# --- introspection ------------------------------------------------------- #


async def stats() -> VoleStatsDTO:
    return await get_service().stats()


# --- live subscription (Hare integration) -------------------------------- #


async def subscribe_live(pvs: list[str]) -> None:
    """Subscribe Vole to live updates from Hare for these PVs."""
    await get_ingest().subscribe(pvs)


async def subscribe_latest(pvs: list[str]) -> None:
    """Subscribe to live updates with a size-1 ring per PV (latest value only).

    Used by scalar formula subscriptions that only need read_latest —
    avoids accumulating a full ring buffer per PV. Never lowers an existing
    ring's capacity. The ring is keyed by the canonical name, the key ingest
    appends under, so a ``ca://X`` binding seeds ring ``X`` and no orphan.
    """
    ingest = get_ingest()
    service = get_service()
    for pv in pvs:
        await service.seed(strip_protocol_prefix(pv), [], max_points=1)
    await ingest.subscribe(pvs)


async def unsubscribe_live(pvs: list[str]) -> None:
    """Unsubscribe from live updates for these PVs."""
    await get_ingest().unsubscribe(pvs)


# --- change notifications ------------------------------------------------ #


def on_change(pv: str, callback: Callable[[str], Awaitable[None]]) -> Callable[[], None]:
    """Subscribe to notifications when a PV ring receives new data.

    Returns an unsubscribe function. Used by the Formula Engine to trigger re-evaluation.
    """
    return get_notifier().subscribe(pv, callback)
