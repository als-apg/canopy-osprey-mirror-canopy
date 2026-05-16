from fastapi import Security, APIRouter

from canopy.core.security import require_read_access
from canopy.mole.schemas.archiver import ArchiverListDTO
from canopy.mole.dependencies import ArchiverServiceDep

router = APIRouter(prefix="/archivers", tags=["Mole / Archivers"])


@router.get("", dependencies=[Security(require_read_access)])
async def list_archivers(service: ArchiverServiceDep) -> ArchiverListDTO:
    """List all configured archivers with current health."""
    return await service.list_archivers()
