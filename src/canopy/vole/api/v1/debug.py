from fastapi import Security, APIRouter

from canopy.core.security import require_read_access, require_admin_access
from canopy.vole.dependencies import BufferServiceDep
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
async def evict_pv(pv: str, service: BufferServiceDep) -> dict[str, str]:
    await service.drop(pv)
    return {"status": "dropped", "pv": pv}
