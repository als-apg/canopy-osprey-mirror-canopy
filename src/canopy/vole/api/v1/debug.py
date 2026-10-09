from fastapi import Security, APIRouter

from canopy.core.security import require_read_access, require_admin_access
from canopy.epics.pv_protocol import strip_protocol_prefix
from canopy.vole.dependencies import BufferServiceDep, IngestServiceDep
from canopy.vole.schemas.buffer import VoleStatsDTO

router = APIRouter(prefix="/_debug", tags=["Vole / Debug"])


@router.get(
    "/buffers",
    dependencies=[Security(require_read_access)],
)
async def list_buffers(service: BufferServiceDep) -> VoleStatsDTO:
    return await service.stats()


@router.post(
    "/evict/{pv:path}",
    dependencies=[Security(require_admin_access)],
)
async def evict_pv(pv: str, service: BufferServiceDep, ingest: IngestServiceDep) -> dict[str, str]:
    # The ring is keyed by the canonical name; every subscribed name feeding it is
    # released first, so no live feed survives its ring.
    canonical = strip_protocol_prefix(pv)
    await ingest.unsubscribe([name for name in ingest.subscribed_pvs if strip_protocol_prefix(name) == canonical])
    await service.drop(canonical)
    return {"status": "dropped", "pv": pv}
