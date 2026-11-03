"""Facility hierarchy endpoints.

Writes require the facility wildcard, not just ``admin`` — stricter than key
management (see ``core/keys/routes.py``), because reparenting or deleting a node
can widen what every *existing* grant on an ancestor resolves to, not just the
grant the acting key is minting. A facility-scoped admin delegating a narrower
*key* grant is a separate check, in ``core.keys.service``. ``GET`` accepts
``read`` or ``admin`` — facility *existence* isn't a secret, and an admin-only
provisioning key needs to see what it just defined without also being handed an
unrelated data-plane scope — which is why ``Security`` is per-route rather than
on the router.

Write routes are keyed by slug — it's immutable, and it's how a facility is spelled
everywhere else (``?facility=``, key grants). ``FacilityService`` takes ids, so each
route resolves the slug first.
"""

from fastapi import Security, APIRouter

from canopy.core.errors import FacilityNotFound
from canopy.core.security import (
    require_read_or_admin_access,
    require_wildcard_facility_access,
)
from canopy.core.facilities.schemas import FacilityDTO, FacilityCreate, FacilityUpdate
from canopy.core.facilities.dependencies import FacilityServiceDep

router = APIRouter(prefix="/v1/facilities", tags=["Facilities"])


@router.get("", dependencies=[Security(require_read_or_admin_access)])
async def list_facilities(service: FacilityServiceDep) -> list[FacilityDTO]:
    """Full facility hierarchy, flat with ``parent_id`` — the client assembles the tree."""
    return await service.get_all()


@router.post("", dependencies=[Security(require_wildcard_facility_access)])
async def create_facility(data: FacilityCreate, service: FacilityServiceDep) -> FacilityDTO:
    """Define a facility. ``slug`` is immutable once created — key grants and
    ``?facility=`` filters reference it, so renaming would silently invalidate them."""
    return await service.create(data)


@router.put("/{facility_slug}", dependencies=[Security(require_wildcard_facility_access)])
async def update_facility(facility_slug: str, data: FacilityUpdate, service: FacilityServiceDep) -> FacilityDTO:
    """Update a facility's name, description, or parent. ``slug`` is not editable."""
    facility = await service.get_by_slug(facility_slug)
    if not facility:
        raise FacilityNotFound(f"Facility '{facility_slug}' not found")
    return await service.update(facility.id, data)


@router.delete("/{facility_slug}", dependencies=[Security(require_wildcard_facility_access)])
async def delete_facility(facility_slug: str, service: FacilityServiceDep) -> dict[str, bool]:
    """Delete a facility. Refused while it has children, or while any module's data or
    any active key's grant still references it."""
    facility = await service.get_by_slug(facility_slug)
    if not facility:
        raise FacilityNotFound(f"Facility '{facility_slug}' not found")
    await service.delete(facility.id)
    return {"deleted": True}
