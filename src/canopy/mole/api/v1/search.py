from typing import Annotated

from fastapi import Query, Security, APIRouter

from canopy.core.security import require_read_access
from canopy.mole.schemas.search import SearchResultsDTO
from canopy.mole.dependencies import ArchiverServiceDep

router = APIRouter(prefix="/search", tags=["Mole / Search"])


@router.get("", dependencies=[Security(require_read_access)])
async def search(
    service: ArchiverServiceDep,
    pattern: Annotated[str, Query(description="Archiver-native pattern; Appliance uses regex")],
    archiver: Annotated[str | None, Query(description="Instance name; default if omitted")] = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
) -> SearchResultsDTO:
    """Search a single archiver's PV index by name pattern.

    Returns PV names only; for full metadata, call `/metadata/{pv}` per result.
    """
    archiver_name, pvs = await service.search(pattern, archiver=archiver, limit=limit)
    return SearchResultsDTO(archiver=archiver_name, pattern=pattern, pvs=pvs)
