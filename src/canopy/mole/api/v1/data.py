from typing import Annotated, Literal

from fastapi import Path, Query, Security, APIRouter

from canopy.core.errors import BadRequest
from canopy.core.security import require_read_access
from canopy.mole.schemas.series import SampleDTO, ArchiverSeriesDTO
from canopy.mole.dependencies import ArchiverServiceDep
from canopy.mole.api.v1._time_parse import now_ns, parse_time

router = APIRouter(prefix="/data", tags=["Mole / Data"])


@router.get("/{pv}", dependencies=[Security(require_read_access)])
async def get_data(
    pv: Annotated[str, Path(description="PV name; URL-encode if it contains special characters")],
    service: ArchiverServiceDep,
    from_: Annotated[str, Query(alias="from", description="ISO-8601 or epoch-ns")],
    to: Annotated[str | None, Query(description="ISO-8601 or epoch-ns; omit for 'now'")] = None,
    archiver: Annotated[str | None, Query(description="Instance name; default if omitted")] = None,
    binning: Annotated[Literal["auto", "time"] | None, Query(description="Switches to binned mode")] = None,
    max_points: Annotated[
        int | None, Query(alias="maxPoints", ge=1, description="Decimation cap or bucket count")
    ] = None,
    bin_size: Annotated[str | None, Query(alias="binSize", description="Required when binning='time'")] = None,
) -> ArchiverSeriesDTO:
    """Fetch one PV's time-window data.

    Raw mode by default. Binned mode (`binning=auto|time`) lands in Commit 6.
    """
    if binning is not None:
        # Surface a clear 400 until binned mode is implemented in the next commit.
        raise BadRequest("binned mode is not yet implemented (lands with binning support)")

    t0_ns = parse_time(from_)
    if t0_ns is None:
        raise BadRequest("`from` is required")
    t1_ns = parse_time(to) if to else now_ns()
    assert t1_ns is not None
    if t0_ns > t1_ns:
        raise BadRequest("`from` must be ≤ `to`")

    archiver_name, samples, decimated = await service.fetch_window(
        pv, t0_ns, t1_ns, archiver=archiver, max_points=max_points
    )
    return ArchiverSeriesDTO(
        pv=pv,
        archiver=archiver_name,
        from_ns=t0_ns,
        to_ns=t1_ns,
        decimated=decimated,
        samples=[SampleDTO(**s.model_dump()) for s in samples],
    )
