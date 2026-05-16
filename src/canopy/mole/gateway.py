"""Public surface of the Mole module for in-process callers.

Other Canopy modules import only from this file; the rest of `canopy.mole.*`
is private implementation. Each function is a thin facade over `ArchiverService`.

`bind()` wires the service singleton at app startup; `get_service()` is exposed
for `dependencies.py`. Tests can re-bind a fake service freely.
"""

from typing import Literal

from canopy.contracts.sample import Sample, BinnedPoint
from canopy.mole.schemas.pv import PVMetadata
from canopy.mole.schemas.archiver import ArchiverListDTO
from canopy.mole.services.archiver_service import ArchiverService

_service: ArchiverService | None = None


def bind(service: ArchiverService | None) -> None:
    """Wire (or unwire) the ArchiverService singleton."""
    global _service
    _service = service


def get_service() -> ArchiverService:
    if _service is None:
        raise RuntimeError("Mole gateway accessed before app startup bound a service")
    return _service


# --------------------------------------------------------------------------- #
# Public facade — one function per service method. Other modules import these.
# --------------------------------------------------------------------------- #


async def fetch_window(
    pv: str,
    t0_ns: int,
    t1_ns: int,
    *,
    archiver: str | None = None,
    max_points: int | None = None,
) -> list[Sample]:
    """Fetch one PV's raw samples from one archiver in `[t0_ns, t1_ns]`.

    `archiver=None` selects the configured default. The gateway returns just
    the samples; callers that need the resolved archiver name should call the
    HTTP route or `ArchiverService.fetch_window` directly.
    """
    _, samples, _ = await get_service().fetch_window(pv, t0_ns, t1_ns, archiver=archiver, max_points=max_points)
    return samples


async def fetch_binned(
    pv: str,
    t0_ns: int,
    t1_ns: int,
    *,
    binning: Literal["auto", "time"],
    archiver: str | None = None,
    target_buckets: int | None = None,
    bin_size_ns: int | None = None,
) -> list[BinnedPoint]:
    """Fetch one PV's pre-binned statistics from one archiver.

    See `ArchiverService.fetch_binned` for argument details. Raises
    `BinningNotSupported` (501) when the adapter cannot honor the requested
    mode.
    """
    _, points = await get_service().fetch_binned(
        pv,
        t0_ns,
        t1_ns,
        archiver=archiver,
        binning=binning,
        target_buckets=target_buckets,
        bin_size_ns=bin_size_ns,
    )
    return points


async def metadata(pv: str, *, archiver: str | None = None) -> PVMetadata:
    """Per-PV metadata as known to the archiver."""
    return await get_service().metadata(pv, archiver=archiver)


async def search(
    pattern: str,
    *,
    archiver: str | None = None,
    limit: int = 100,
) -> list[str]:
    """Pattern-search a single archiver's PV index. Returns names only."""
    _, pvs = await get_service().search(pattern, archiver=archiver, limit=limit)
    return pvs


async def list_archivers() -> ArchiverListDTO:
    """Return all configured archivers with current health."""
    return await get_service().list_archivers()
