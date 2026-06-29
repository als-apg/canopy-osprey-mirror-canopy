from typing import Annotated

from fastapi import Path, Query, Security, APIRouter

from canopy.core.errors import BadRequest
from canopy.vole.errors import PVNotBuffered
from canopy.core.security import require_read_access
from canopy.core.time_parse import parse_time
from canopy.vole.dependencies import BufferServiceDep
from canopy.vole.schemas.buffer import PVSamplesDTO

router = APIRouter(prefix="/pvs", tags=["Vole / PVs"])


@router.get("/{pv:path}", dependencies=[Security(require_read_access)])
async def single_pv_read(
    pv: Annotated[str, Path(description="PV name; URL-encode if it contains special characters")],
    service: BufferServiceDep,
    from_: Annotated[str, Query(alias="from", description="ISO-8601 or epoch-ns")],
    to: Annotated[str | None, Query(default=None, description="ISO-8601 or epoch-ns")],
    align: Annotated[
        str,
        Query(default="last", description="'last' (default) pins a pre-window sample at t0; 'none' returns raw slice"),
    ],
    max_points: Annotated[
        int | None,
        Query(default=None, alias="maxPoints", ge=1, description="Decimation cap (raw) or bucket count (binning=auto)"),
    ],
):
    t0_ns = parse_time(from_)
    if t0_ns is None:
        raise BadRequest("'from' is required")
    t1_ns = parse_time(to)
    if t1_ns is None:
        raise BadRequest("'to' is invalud")
    if t0_ns > t1_ns:
        raise BadRequest("'from' must be ≤ 'to'")

    try:
        samples = await service.read_window(pv, t0_ns, t1_ns, align=align, max_points=max_points)

        return PVSamplesDTO(pv=pv, count=len(samples), samples=samples)
    except PVNotBuffered:
        raise
