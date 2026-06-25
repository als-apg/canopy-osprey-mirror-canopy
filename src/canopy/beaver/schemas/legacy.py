"""DTOs for legacy ``.ui`` / ``.py`` serving.

Beaver does not parse these files; it serves bytes. Paths in the listing are
relative to ``BeaverConfig.legacy_screens_dir`` and use forward slashes
regardless of platform.
"""

from canopy.contracts.base import CanopyModel


class LegacyScreenSummaryDTO(CanopyModel):
    path: str
    size_bytes: int
    modified_ts_ns: int


class LegacyScreenListDTO(CanopyModel):
    screens: list[LegacyScreenSummaryDTO]
    total: int
