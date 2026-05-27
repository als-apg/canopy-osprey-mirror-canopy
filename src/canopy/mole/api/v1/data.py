from typing import Literal, Annotated

from fastapi import Path, Query, Security, APIRouter

from canopy.core.errors import BadRequest
from canopy.core.security import require_read_access
from canopy.mole.dependencies import ArchiverServiceDep
from canopy.mole.schemas.series import (
    SampleDTO,
    BinnedPointDTO,
    ArchiverSeriesDTO,
    ArchiverBinnedSeriesDTO,
)
from canopy.mole.api.v1._time_parse import now_ns, parse_time, parse_bin_size_to_ns

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
        int | None, Query(alias="maxPoints", ge=1, description="Decimation cap (raw) or bucket count (binning=auto)")
    ] = None,
    bin_size: Annotated[
        str | None, Query(alias="binSize", description="Required when binning='time'; nanoseconds as integer")
    ] = None,
) -> ArchiverSeriesDTO | ArchiverBinnedSeriesDTO:
    """Fetch one PV's time-window data.

    Raw mode by default (returns `ArchiverSeriesDTO`). Binned mode when
    `binning` is set (returns `ArchiverBinnedSeriesDTO`). Branch on `mode`.
    """
    t0_ns = parse_time(from_)
    if t0_ns is None:
        raise BadRequest("`from` is required")
    t1_ns = parse_time(to) if to else now_ns()
    if t1_ns is None:
        raise BadRequest("`to` is invalid")
    if t0_ns > t1_ns:
        raise BadRequest("`from` must be ≤ `to`")

    if binning is None:
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

    # Binned mode.
    target_buckets: int | None = None
    bin_size_ns: int | None = None
    if binning == "auto":
        if max_points is None:
            raise BadRequest("`binning='auto'` requires `maxPoints` (target bucket count)")
        target_buckets = max_points
    elif binning == "time":
        if bin_size is None:
            raise BadRequest("`binning='time'` requires `binSize`")
        bin_size_ns = parse_bin_size_to_ns(bin_size)

    archiver_name, points = await service.fetch_binned(
        pv,
        t0_ns,
        t1_ns,
        archiver=archiver,
        binning=binning,
        target_buckets=target_buckets,
        bin_size_ns=bin_size_ns,
    )
    return ArchiverBinnedSeriesDTO(
        pv=pv,
        archiver=archiver_name,
        from_ns=t0_ns,
        to_ns=t1_ns,
        binning=binning,
        bin_size_ns=bin_size_ns,
        target_buckets=target_buckets,
        points=[BinnedPointDTO(**p.model_dump()) for p in points],
    )
