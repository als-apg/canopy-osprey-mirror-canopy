"""Snapshot endpoints.

Create and restore are long-running EPICS I/O, so they enqueue an Arq job and return a
``JobCreatedDTO`` for the client to poll at ``/jobs/v1/{id}`` — the worker holds the EPICS
connector. Everything else (list / get / update / delete / compare) runs inline against the
request session.
"""

from fastapi import Query, Security, APIRouter

from canopy.jobs import gateway as jobs_gateway
from canopy.contracts.jobs import JobCreatedDTO
from canopy.core.security import require_read_access, require_write_access
from canopy.squirrel.errors import SnapshotNotFound
from canopy.squirrel.dependencies import SnapshotServiceDep
from canopy.squirrel.schemas.snapshot import (
    SnapshotDTO,
    NewSnapshotDTO,
    RestoreRequestDTO,
    SnapshotSummaryDTO,
    UpdateSnapshotDTO,
    ComparisonResultDTO,
)
from canopy.squirrel.api.v1._validation import is_uuid

router = APIRouter(prefix="/snapshots", tags=["Snapshots"])


@router.get("", dependencies=[Security(require_read_access)])
async def list_snapshots(
    service: SnapshotServiceDep,
    title: str | None = Query(None),
    tags: list[str] = Query(default=[], description="Filter by tag IDs"),
) -> list[SnapshotSummaryDTO]:
    """List snapshots, optionally filtered by title and/or tag IDs."""
    return await service.list_snapshots(title=title, tag_ids=tags or None)


@router.post("", dependencies=[Security(require_write_access)])
async def create_snapshot(data: NewSnapshotDTO) -> JobCreatedDTO:
    """Enqueue a snapshot of every registered PV. The worker reads EPICS and persists it."""
    job_id = await jobs_gateway.enqueue("create_snapshot_task", title=data.title, description=data.description)
    return JobCreatedDTO(job_id=job_id, status_url=f"/jobs/v1/{job_id}")


@router.get("/{snapshot_id}", dependencies=[Security(require_read_access)])
async def get_snapshot(
    snapshot_id: str,
    service: SnapshotServiceDep,
    limit: int | None = Query(None, ge=1, description="Max PV values to return"),
    offset: int = Query(0, ge=0),
) -> SnapshotDTO:
    """Get one snapshot with its PV values (values optionally paginated)."""
    if not is_uuid(snapshot_id):
        raise SnapshotNotFound(f"Snapshot {snapshot_id} not found")
    snapshot = await service.get_by_id(snapshot_id, limit=limit, offset=offset)
    if not snapshot:
        raise SnapshotNotFound(f"Snapshot {snapshot_id} not found")
    return snapshot


@router.put("/{snapshot_id}", dependencies=[Security(require_write_access)])
async def update_snapshot(
    snapshot_id: str,
    data: UpdateSnapshotDTO,
    service: SnapshotServiceDep,
) -> SnapshotSummaryDTO:
    """Update a snapshot's title and/or description."""
    if not is_uuid(snapshot_id):
        raise SnapshotNotFound(f"Snapshot {snapshot_id} not found")
    snapshot = await service.update_snapshot_metadata(snapshot_id, title=data.title, description=data.description)
    if not snapshot:
        raise SnapshotNotFound(f"Snapshot {snapshot_id} not found")
    return snapshot


@router.delete("/{snapshot_id}", dependencies=[Security(require_write_access)])
async def delete_snapshot(snapshot_id: str, service: SnapshotServiceDep) -> bool:
    """Delete a snapshot and its values."""
    if not is_uuid(snapshot_id):
        raise SnapshotNotFound(f"Snapshot {snapshot_id} not found")
    if not await service.delete_snapshot(snapshot_id):
        raise SnapshotNotFound(f"Snapshot {snapshot_id} not found")
    return True


@router.post("/{snapshot_id}/restore", dependencies=[Security(require_write_access)])
async def restore_snapshot(
    snapshot_id: str,
    service: SnapshotServiceDep,
    request: RestoreRequestDTO | None = None,
) -> JobCreatedDTO:
    """Enqueue a restore of a snapshot's setpoints to EPICS (``pv_ids=None`` restores all)."""
    if not is_uuid(snapshot_id) or not await service.snapshot_exists(snapshot_id):
        raise SnapshotNotFound(f"Snapshot {snapshot_id} not found")
    pv_ids = request.pv_ids if request else None
    job_id = await jobs_gateway.enqueue("restore_snapshot_task", snapshot_id=snapshot_id, pv_ids=pv_ids)
    return JobCreatedDTO(job_id=job_id, status_url=f"/jobs/v1/{job_id}")


@router.get("/{snapshot_id1}/compare/{snapshot_id2}", dependencies=[Security(require_read_access)])
async def compare_snapshots(
    snapshot_id1: str,
    snapshot_id2: str,
    service: SnapshotServiceDep,
) -> ComparisonResultDTO:
    """Compare two stored snapshots value-by-value (within per-PV tolerance)."""
    for sid in (snapshot_id1, snapshot_id2):
        if not is_uuid(sid):
            raise SnapshotNotFound(f"Snapshot {sid} not found")
    return await service.compare_snapshots(snapshot_id1, snapshot_id2)
