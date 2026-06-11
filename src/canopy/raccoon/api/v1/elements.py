from typing import Annotated

from fastapi import Query, Security, APIRouter

from canopy.core.security import require_read_access
from canopy.raccoon.cache.keys import NameKind
from canopy.raccoon.dependencies import DirectoryServiceDep
from canopy.raccoon.schemas.directory import NameListResultDTO

router = APIRouter(prefix="/elements", tags=["Raccoon / Elements"])


@router.get("", dependencies=[Security(require_read_access)])
async def list_elements(
    service: DirectoryServiceDep,
    pattern: Annotated[str, Query(description="Upstream-native pattern")],
    tag: Annotated[str | None, Query()] = None,
    sort_by: Annotated[str | None, Query(alias="sortBy")] = None,
    element_type: Annotated[str | None, Query(alias="elementType")] = None,
) -> NameListResultDTO:
    names = await service.list(NameKind.ELEMENT, pattern, tag=tag, sort_by=sort_by, element_type=element_type)
    return NameListResultDTO(names=names)
