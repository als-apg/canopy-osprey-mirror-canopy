"""Public surface of the Raccoon module for in-process callers.

Other Canopy modules import only from this file; the rest of `canopy.raccoon.*`
is private implementation. The signatures here form the contract that survives
internal refactoring.

Pattern semantics (Oracle-style `%` wildcards or regex) are defined by the
upstream directory service. Raccoon passes patterns through verbatim; callers
are responsible for understanding what their pattern means.

`tag`, `sort_by`, and `element_type` are opaque strings; Raccoon does not
validate them against any taxonomy. The upstream rejects unknown values, and
Raccoon surfaces the rejection as `InvalidQuery`.
"""

from canopy.raccoon.cache.keys import NameKind
from canopy.raccoon.schemas.directory import RaccoonStatsDTO, ValidateResultDTO
from canopy.raccoon.services.directory_service import DirectoryService

_service: DirectoryService | None = None


def bind(service: DirectoryService | None) -> None:
    """Wire (or unwire) the DirectoryService singleton."""
    global _service
    _service = service


def get_service() -> DirectoryService:
    if _service is None:
        raise RuntimeError("Raccoon gateway accessed before app startup bound a service")
    return _service


# --------------------------------------------------------------------------- #
# Listing -- pattern queries
# --------------------------------------------------------------------------- #


async def list_pvs(
    pattern: str,
    *,
    tag: str | None = None,
    sort_by: str | None = None,
    element_type: str | None = None,
) -> list[str]:
    """Resolve a pattern to a list of PV names. See spec §9 for cache semantics."""
    return await get_service().list(
        NameKind.PV, pattern, tag=tag, sort_by=sort_by, element_type=element_type
    )


async def list_devices(
    pattern: str,
    *,
    tag: str | None = None,
    sort_by: str | None = None,
    element_type: str | None = None,
) -> list[str]:
    return await get_service().list(
        NameKind.DEVICE, pattern, tag=tag, sort_by=sort_by, element_type=element_type
    )


async def list_elements(
    pattern: str,
    *,
    tag: str | None = None,
    sort_by: str | None = None,
    element_type: str | None = None,
) -> list[str]:
    return await get_service().list(
        NameKind.ELEMENT, pattern, tag=tag, sort_by=sort_by, element_type=element_type
    )


# --------------------------------------------------------------------------- #
# Existence -- single and batch
# --------------------------------------------------------------------------- #


async def exists(pv: str) -> bool:
    """Return True if `pv` is known to the directory service.

    `pv` MUST be a literal PV name. If it contains `%`, `*`, `?`, `(`, `|`, or
    `[`, `InvalidQuery` is raised; use `list_pvs` for pattern queries.
    """
    return await get_service().exists(pv)


async def validate(pvs: list[str]) -> ValidateResultDTO:
    """Batch existence check for a list of literal PV names.

    Splits `pvs` into hits and misses against the existence cache, fans out
    misses through `exists` with bounded concurrency, and returns a partition
    that preserves the caller's input order in each list.
    """
    return await get_service().validate(pvs)


# --------------------------------------------------------------------------- #
# Introspection
# --------------------------------------------------------------------------- #


async def stats() -> RaccoonStatsDTO:
    """Return cache hit rates, sizes, in-flight counts, and adapter health."""
    return await get_service().stats()
