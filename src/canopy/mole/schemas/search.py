from canopy.contracts.base import CanopyModel


class SearchResultsDTO(CanopyModel):
    """Response for `GET /mole/v1/search`. Returns PV names only.

    For the Appliance adapter this is a thin wrapper around the regex search
    endpoint, which is the only metadata it carries for search results. Full
    metadata is available per-PV via `GET /metadata/{pv}`.
    """

    archiver: str
    pattern: str
    pvs: list[str]
