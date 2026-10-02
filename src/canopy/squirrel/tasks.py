"""Arq worker tasks for snapshot create / restore.

Run in the ``canopy-worker`` process off the request path. Progress here rides a
per-job Redis key via ``jobs.gateway.report_progress`` (Arq passes the pool as
``ctx['redis']`` and the id as ``ctx['job_id']``), a restore's per-phase progress
included, and the whole DB write is one ``session_scope`` transaction committed once
at the end — so the snapshot row and its COPY'd values are all-or-nothing and an Arq
retry never resumes against partial state.

The EPICS connector is built once at worker startup and shared via ``ctx['epics']``.
Task results are opaque refs the Jobs module returns verbatim to pollers.
"""

from __future__ import annotations

import logging

from arq import Retry

from canopy.core.db import session_scope
from canopy.jobs.gateway import report_progress
from canopy.squirrel.config import get_squirrel_config
from canopy.squirrel.schemas.snapshot import NewSnapshotDTO, RestoreRequestDTO
from canopy.squirrel.adapters.epics_io import EpicsIO
from canopy.squirrel.services.snapshot_service import SnapshotService

logger = logging.getLogger(__name__)

# Substring markers in an error message that make a failure worth retrying.
_TRANSIENT_MARKERS = ("connection", "timeout", "database")


def _build_epics_io(ctx: dict) -> EpicsIO:
    cfg = get_squirrel_config()
    return EpicsIO(
        ctx["epics"],
        concurrency=cfg.epics_concurrency,
        chunk_size=cfg.epics_chunk_size,
        timeout=cfg.epics_timeout_s,
    )


def _progress_sink(ctx: dict):
    redis = ctx["redis"]
    job_id = ctx["job_id"]

    async def on_progress(current: int, total: int, message: str, phases: list[dict] | None = None) -> None:
        percent = int((current / total) * 100) if total else 0
        await report_progress(redis, job_id, percent, message, phases=phases)

    return on_progress


def _maybe_retry(exc: Exception) -> None:
    if any(marker in str(exc).lower() for marker in _TRANSIENT_MARKERS):
        logger.info("Retrying task after transient error: %s", exc)
        raise Retry(defer=10) from exc


async def create_snapshot_task(
    ctx: dict, title: str, description: str | None = None, created_by: str | None = None
) -> dict:
    """Read every registered PV from EPICS and persist a snapshot. Returns ``{"type":"snapshot","id":...}``."""
    logger.info("create_snapshot_task: job_id=%s title=%r", ctx.get("job_id"), title)
    try:
        epics_io = _build_epics_io(ctx)
        async with session_scope() as session:
            service = SnapshotService(session, epics_io)
            summary = await service.create_snapshot(
                NewSnapshotDTO(title=title, description=description),
                created_by=created_by,
                progress_callback=_progress_sink(ctx),
            )
            await session.commit()
        return {"type": "snapshot", "id": summary.id}
    except Exception as exc:
        logger.exception("create_snapshot_task failed: job_id=%s", ctx.get("job_id"))
        _maybe_retry(exc)
        raise


async def restore_snapshot_task(ctx: dict, snapshot_id: str, pv_ids: list[str] | None = None) -> dict:
    """Restore a snapshot's setpoints to EPICS. Returns the restore summary (opaque to Jobs)."""
    logger.info("restore_snapshot_task: job_id=%s snapshot_id=%s", ctx.get("job_id"), snapshot_id)
    try:
        epics_io = _build_epics_io(ctx)
        request = RestoreRequestDTO(pv_ids=pv_ids) if pv_ids else None
        async with session_scope() as session:  # read-only (restore writes to EPICS, not the DB)
            service = SnapshotService(session, epics_io)
            result = await service.restore_snapshot(snapshot_id, request, progress_callback=_progress_sink(ctx))
        return {
            "type": "restore",
            "snapshotId": snapshot_id,
            "totalPvs": result.total_pvs,
            "successCount": result.success_count,
            "failureCount": result.failure_count,
            "failures": result.failures[:50],
        }
    except Exception as exc:
        logger.exception("restore_snapshot_task failed: job_id=%s", ctx.get("job_id"))
        _maybe_retry(exc)
        raise
