from typing import Annotated

from fastapi import Body, Path, Query, Security, APIRouter

from canopy.raccoon.cache.keys import NameKind
from canopy.core.security import require_read_access
from canopy.raccoon.dependencies import DirectoryServiceDep
from canopy.raccoon.schemas.directory import (
    ExistsResultDTO,
    NameListResultDTO,
    ValidateRequestDTO,
    ValidateResultDTO,
)

router = APIRouter(prefix="/pvs", tags=["Raccoon / PVs"])


@router.get("", dependencies=[Security(require_read_access)])
async def list_pvs(
    service: DirectoryServiceDep,
    pattern: Annotated[str, Query(description="Upstream-native pattern (Oracle wildcard or regex)")],
    tag: Annotated[str | None, Query(description="Opaque tag filter; passed through verbatim")] = None,
    sort_by: Annotated[str | None, Query(alias="sortBy", description="Opaque sort key")] = None,
    element_type: Annotated[
        str | None,
        Query(alias="elementType", description="Opaque element-type filter"),
    ] = None,
) -> NameListResultDTO:
    """Resolve a pattern to a list of PV names. Cached per `(pattern, tag, sortBy, elementType)`."""
    names = await service.list(NameKind.PV, pattern, tag=tag, sort_by=sort_by, element_type=element_type)
    return NameListResultDTO(names=names)


@router.get("/{pv}/exists", dependencies=[Security(require_read_access)])
async def exists(
    service: DirectoryServiceDep,
    pv: Annotated[str, Path(description="Literal PV name; pattern metacharacters are rejected")],
) -> ExistsResultDTO:
    """Return whether a single PV is known to the directory service."""
    return ExistsResultDTO(pv=pv, exists=await service.exists(pv))


@router.post("/validate", dependencies=[Security(require_read_access)])
async def validate(
    service: DirectoryServiceDep,
    body: Annotated[ValidateRequestDTO, Body(...)],
) -> ValidateResultDTO:
    """Batch existence check. Input order is preserved in `known` and `unknown`."""
    return await service.validate(body.pvs)
