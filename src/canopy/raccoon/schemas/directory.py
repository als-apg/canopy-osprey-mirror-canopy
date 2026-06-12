"""Wire DTOs for the Raccoon API.

All inherit `CanopyModel`, which renders snake_case attributes as camelCase on
the wire and accepts either form on input. No per-field aliases needed.
"""

from pydantic import Field

from canopy.contracts.base import CanopyModel
from canopy.raccoon.adapters.base import AdapterHealth


class NameListResultDTO(CanopyModel):
    names: list[str]


class ExistsResultDTO(CanopyModel):
    pv: str
    exists: bool


class ValidateRequestDTO(CanopyModel):
    """Batch existence-check request.

    Capped at 10k names — sized for the reconnect-storm use case (spec §5)
    while keeping a single request's memory and fan-out bounded. Larger sets
    should be chunked by the caller.
    """

    pvs: list[str] = Field(max_length=10_000)


class ValidateResultDTO(CanopyModel):
    """Partition of the input list into known and unknown PVs.

    Each list preserves the caller's input order so downstream code can match
    by index.
    """

    known: list[str]
    unknown: list[str]


class CacheStatsDTO(CanopyModel):
    size: int
    hits: int
    misses: int
    evictions: int


class RaccoonStatsDTO(CanopyModel):
    adapter_name: str
    adapter_type: str
    adapter_health: AdapterHealth
    pattern_cache: CacheStatsDTO
    name_cache: CacheStatsDTO
    in_flight: int
