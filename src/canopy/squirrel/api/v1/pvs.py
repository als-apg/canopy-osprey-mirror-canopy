"""PV registry + meta-PV endpoints.

Hare/Vole own live values and the EPICS module owns connectivity probing. So all we need here is
the PV registry: search (keyset + offset), CRUD, bulk import, device list, and meta-PVs.
"""

import json

from fastapi import Query, Security, APIRouter

from canopy.core.errors import BadRequest
from canopy.squirrel.errors import PvNotFound
from canopy.contracts.pagination import PagedResult
from canopy.squirrel.schemas.pv import (
    PVElementDTO,
    NewPVElementDTO,
    PVSearchResponse,
    UpdatePVElementDTO,
)
from canopy.core.security import require_read_access, require_write_access
from canopy.squirrel.dependencies import PvServiceDep
from canopy.squirrel.schemas.meta_pv import (
    MetaPVDTO,
    NewMetaPVDTO,
    DisplayMetaPVDTO,
    UpdateMetaPVDTO,
)
from canopy.squirrel.api.v1._validation import is_uuid

router = APIRouter(prefix="/pvs", tags=["PVs"])


@router.get("", dependencies=[Security(require_read_access)])
async def search_pvs(
    service: PvServiceDep,
    pvName: str | None = Query(None),
) -> list[PVElementDTO]:
    """Search PVs by name (non-paginated, capped — kept for backward compatibility)."""
    result = await service.search_paged(search=pvName, page_size=1000)
    return result.results


@router.get("/paged", dependencies=[Security(require_read_access)])
async def search_pvs_paged(
    service: PvServiceDep,
    pvName: str | None = Query(None),
    pageSize: int = Query(100, ge=1, le=1000),
    continuationToken: str | None = Query(None),
    tagFilters: str | None = Query(None, description='JSON object: {"groupId": ["tagId", ...], ...}'),
) -> PagedResult[PVElementDTO]:
    """Keyset-paginated search with optional tag filtering.

    ``tagFilters`` is ``{group: [tag, ...]}`` — a PV matches when it has at least one tag
    from every listed group (OR within a group, AND across groups).
    """
    tag_filters = None
    if tagFilters:
        try:
            tag_filters = json.loads(tagFilters) or None
        except json.JSONDecodeError as e:
            raise BadRequest(f"Invalid tagFilters JSON: {e}") from e

    return await service.search_paged(
        search=pvName,
        page_size=pageSize,
        continuation_token=continuationToken,
        tag_filters=tag_filters,
    )


@router.get("/search", dependencies=[Security(require_read_access)])
async def search_pvs_filtered(
    service: PvServiceDep,
    q: str | None = Query(None, description="Text search"),
    devices: list[str] = Query(default=[], description="Filter by device"),
    tags: list[str] = Query(default=[], description="Filter by tag IDs"),
    limit: int = Query(100, ge=1, le=1000, description="Max results"),
    offset: int = Query(0, ge=0, description="Offset for pagination"),
) -> PVSearchResponse:
    """Offset-paginated, server-side filtered search (more efficient than client-side filtering)."""
    return await service.search_filtered(
        q=q, devices=devices or None, tag_ids=tags or None, limit=limit, offset=offset
    )


@router.get("/devices", dependencies=[Security(require_read_access)])
async def get_all_devices(service: PvServiceDep) -> list[str]:
    """All unique device names, for the frontend's device filter."""
    return await service.get_all_devices()


@router.post("", dependencies=[Security(require_write_access)])
async def create_pv(data: NewPVElementDTO, service: PvServiceDep) -> PVElementDTO:
    """Create a new PV."""
    return await service.create(data)


@router.post("/multi", dependencies=[Security(require_write_access)])
async def create_multiple_pvs(data: list[NewPVElementDTO], service: PvServiceDep) -> list[PVElementDTO]:
    """Bulk-create PVs (CSV import)."""
    return await service.create_many(data)


@router.put("/{pv_id}", dependencies=[Security(require_write_access)])
async def update_pv(pv_id: str, data: UpdatePVElementDTO, service: PvServiceDep) -> PVElementDTO:
    """Update a PV's metadata (addresses are immutable here)."""
    if not is_uuid(pv_id):
        raise PvNotFound(f"PV {pv_id} not found")
    pv = await service.update(pv_id, data)
    if not pv:
        raise PvNotFound(f"PV {pv_id} not found")
    return pv


@router.delete("/{pv_id}", dependencies=[Security(require_write_access)])
async def delete_pv(pv_id: str, service: PvServiceDep) -> bool:
    """Delete a PV."""
    if not is_uuid(pv_id):
        raise PvNotFound(f"PV {pv_id} not found")
    if not await service.delete(pv_id):
        raise PvNotFound(f"PV {pv_id} not found")
    return True


# --- meta PVs (PVs shown specially on snapshots) ---------------------------- #


@router.post("/meta", dependencies=[Security(require_write_access)])
async def add_meta_pv(pvInfo: NewMetaPVDTO, service: PvServiceDep) -> MetaPVDTO:
    """Promote an existing PV (by address) to a meta-PV."""
    return await service.add_meta_pv(pvInfo.pv_name, pvInfo.display_name)


@router.get("/meta", dependencies=[Security(require_read_access)])
async def get_meta_pvs(service: PvServiceDep, isActive: bool | None = None) -> list[DisplayMetaPVDTO]:
    """List meta-PVs, optionally filtered by active state."""
    return await service.get_meta_pvs(active=isActive)


@router.get("/meta/{id}", dependencies=[Security(require_read_access)])
async def get_meta_pv(id: str, service: PvServiceDep) -> MetaPVDTO:
    """Get one meta-PV by its PV id."""
    if not is_uuid(id):
        raise PvNotFound(f"Meta PV {id} not found")
    return await service.get_meta_pv(id)


@router.patch("/meta/{id}", dependencies=[Security(require_write_access)])
async def update_meta_pv(id: str, update: UpdateMetaPVDTO, service: PvServiceDep) -> MetaPVDTO:
    """Update a meta-PV's display name and/or active state."""
    if not is_uuid(id):
        raise PvNotFound(f"Meta PV {id} not found")
    result = None
    if update.display_name is not None:
        result = await service.set_meta_display_name(id=id, display_name=update.display_name)
    if update.active is not None:
        result = await (service.enable_meta_pv(id) if update.active else service.disable_meta_pv(id))
    if result is None:
        result = await service.get_meta_pv(id)
    return result


@router.delete("/meta/{id}", status_code=204, dependencies=[Security(require_write_access)])
async def delete_meta_pv(id: str, service: PvServiceDep) -> None:
    """Demote a meta-PV back to an ordinary PV."""
    if not is_uuid(id):
        raise PvNotFound(f"Meta PV {id} not found")
    await service.delete_meta_pv(id)
