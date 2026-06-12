from typing import Annotated

from fastapi import Path, Security, APIRouter

from canopy.contracts.pv import PVMeta, PVValue
from canopy.core.security import require_read_access
from canopy.hare.dependencies import PVBrokerDep

router = APIRouter(prefix="/pvs", tags=["Hare / PVs"])


@router.get("/{pv:path}/describe", dependencies=[Security(require_read_access)])
async def describe_pv(
    pv: Annotated[str, Path(description="PV name; may carry a ca:// or pva:// prefix")],
    broker: PVBrokerDep,
) -> PVMeta:
    """Static metadata for a PV — value kind, units, precision, enum choices,
    display/control limits, and write access.

    These rarely change, so a client reads them once on connect to configure a
    display, the live value stream carries only the dynamic fields. Served from
    the broker's cache when the PV is actively monitored, else fetched on demand.
    """
    return await broker.get_describe(pv)


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
