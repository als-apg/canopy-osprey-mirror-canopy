import math
from typing import Literal, Annotated

from fastapi import Path, Query, Security, APIRouter

from canopy.core.errors import BadRequest
from canopy.core.security import require_read_access
from canopy.core.time_parse import now_ns, parse_time
from canopy.vole.dependencies import BufferServiceDep
from canopy.vole.schemas.buffer import PVSeriesDTO, AlignedPVDTO, AlignedResultDTO

AlignMode = Literal["last", "nearest", "linear", "none"]
IndexMode = Literal["union", "intersection", "grid", "first_input"]

router = APIRouter(prefix="/pvs", tags=["Vole / PVs"])


@router.get("/", dependencies=[Security(require_read_access)])
async def multi_pv_aligned_read(
    service: BufferServiceDep,
    pvs: Annotated[str, Query(description="Comma-separated PV names")],
    from_: Annotated[str, Query(alias="from", description="ISO-8601 or epoch-ns")],
    to: Annotated[str | None, Query(description="ISO-8601 or epoch-ns")] = None,
    align: Annotated[AlignMode, Query(description="Alignment mode: last, nearest, linear, none")] = "last",
    index: Annotated[IndexMode, Query(description="Index mode: union, intersection, grid, first_input")] = "union",
    grid_dt_ns: Annotated[
        int | None, Query(alias="gridDtNs", ge=1, description="Grid spacing in ns (required when index=grid)")
    ] = None,
):
    pv_list = [p.strip() for p in pvs.split(",") if p.strip()]
    if not pv_list:
        raise BadRequest("'pvs' must contain at least one PV name")

    t0_ns = parse_time(from_)
    if t0_ns is None:
        raise BadRequest("'from' is required")
    t1_ns = parse_time(to) or now_ns()
    if t0_ns > t1_ns:
        raise BadRequest("'from' must be <= 'to'")

    if index == "grid" and grid_dt_ns is None:
        raise BadRequest("'gridDtNs' is required when index='grid'")

    result = await service.read_aligned(pv_list, t0_ns, t1_ns, align=align, index=index, grid_dt_ns=grid_dt_ns)

    return AlignedResultDTO(
        ts_ns=result.ts_ns.tolist(),
        pvs=[
            AlignedPVDTO(
                pv=pv,
                values=[None if math.isnan(v) else v for v in result.arrays[pv].tolist()],
                severities=result.severities[pv].tolist(),
            )
            for pv in pv_list
        ],
    )


@router.get("/{pv:path}", dependencies=[Security(require_read_access)])
async def single_pv_read(
    pv: Annotated[str, Path(description="PV name; URL-encode if it contains special characters")],
    service: BufferServiceDep,
    from_: Annotated[str, Query(alias="from", description="ISO-8601 or epoch-ns")],
    to: Annotated[str | None, Query(description="ISO-8601 or epoch-ns")] = None,
    align: Annotated[
        Literal["last", "none"],
        Query(description="'last' (default) pins a pre-window sample at t0; 'none' returns raw slice"),
    ] = "last",
    max_points: Annotated[
        int | None,
        Query(alias="maxPoints", ge=1, description="Decimation cap; if set, downsample to at most this many points"),
    ] = None,
):
    t0_ns = parse_time(from_)
    if t0_ns is None:
        raise BadRequest("'from' is required")
    t1_ns = parse_time(to) or now_ns()
    if t0_ns > t1_ns:
        raise BadRequest("'from' must be <= 'to'")

    samples = await service.read_window(pv, t0_ns, t1_ns, align=align, max_points=max_points)
    return PVSeriesDTO(pv=pv, count=len(samples), samples=samples)
