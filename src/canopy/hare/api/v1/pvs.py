from typing import Annotated

from fastapi import Path, Security, APIRouter

from canopy.contracts.pv import PVValue
from canopy.core.security import require_read_access
from canopy.hare.dependencies import PVBrokerDep

router = APIRouter(prefix="/pvs", tags=["Hare / PVs"])


@router.get("/{pv:path}", dependencies=[Security(require_read_access)])
async def get_pv(
    pv: Annotated[str, Path(description="PV name; may carry a ca:// or pva:// prefix")],
    broker: PVBrokerDep,
) -> PVValue:
    """Current value of a PV.

    Returns the cached value if the PV is actively monitored, otherwise issues a
    one-shot read through the connector.
    """
    return await broker.get_value(pv)
