"""Cache-key primitives shared by the pattern cache and the directory service.

`NameKind` distinguishes the three list calls (PVs, devices, elements). `CacheKey`
captures the full set of inputs that affect a pattern's result; equality on the
key is the cache identity.
"""

from enum import StrEnum
from dataclasses import dataclass


class NameKind(StrEnum):
    PV = "pv"
    DEVICE = "device"
    ELEMENT = "element"


@dataclass(frozen=True, slots=True)
class CacheKey:
    """The full identity of a pattern lookup.

    Two queries are the same cache entry only if every field matches. Optional
    knobs that the upstream interprets (`tag`, `sort_by`, `element_type`) are
    part of the key because they change the result.
    """

    kind: NameKind
    pattern: str
    tag: str | None = None
    sort_by: str | None = None
    element_type: str | None = None
