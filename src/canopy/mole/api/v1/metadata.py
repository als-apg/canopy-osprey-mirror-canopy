from typing import Annotated

from fastapi import Path, Query, Security, APIRouter

from canopy.core.security import require_read_access
from canopy.mole.schemas.pv import PVMetadataDTO
from canopy.mole.dependencies import ArchiverServiceDep

router = APIRouter(prefix="/metadata", tags=["Mole / Metadata"])


@router.get("/{pv}", dependencies=[Security(require_read_access)])
async def get_metadata(
    pv: Annotated[str, Path(description="PV name; URL-encode if it contains special characters")],
    service: ArchiverServiceDep,
    archiver: Annotated[str | None, Query(description="Instance name; default if omitted")] = None,
) -> PVMetadataDTO:
    """Per-PV metadata as the archiver knows it.

    For the Appliance adapter only `units` and `precision` are populated; the
    rest stay `null` and come from Channel Access (Hare / EPICS Connector).
    """
    return await service.metadata(pv, archiver=archiver)
