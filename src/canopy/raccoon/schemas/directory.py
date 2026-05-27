"""Wire DTOs for the Raccoon API.

All inherit `CanopyModel`, which renders snake_case attributes as camelCase on
the wire and accepts either form on input. No per-field aliases needed.
"""

from canopy.contracts.base import CanopyModel
from canopy.raccoon.adapters.base import AdapterHealth


class ListQueryDTO(CanopyModel):
    """Query parameters shared by `/pvs`, `/devices`, `/elements`.

    `pattern` is required and is passed through to the upstream verbatim. The
    optional knobs are also opaque; Raccoon does not validate their values.
    """

    pattern: str
    tag: str | None = None
    sort_by: str | None = None
    element_type: str | None = None


class NameListResultDTO(CanopyModel):
    names: list[str]


class ExistsResultDTO(CanopyModel):
    pv: str
    exists: bool


class ValidateRequestDTO(CanopyModel):
    pvs: list[str]


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
