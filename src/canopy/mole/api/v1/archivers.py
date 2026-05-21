from fastapi import Security, APIRouter

from canopy.core.security import require_read_access
from canopy.mole.dependencies import ArchiverServiceDep
from canopy.mole.schemas.archiver import ArchiverListDTO

router = APIRouter(prefix="/archivers", tags=["Mole / Archivers"])


@router.get("", dependencies=[Security(require_read_access)])
async def list_archivers(service: ArchiverServiceDep) -> ArchiverListDTO:
    """List all configured archivers with current health."""
    return await service.list_archivers()
