from typing import Literal

from pydantic import Field

from canopy.contracts.base import CanopyModel
from canopy.contracts.sample import AlarmSeverity


class SampleDTO(CanopyModel):
    """One stored sample. `source` is always `"archive"` from Mole."""

    ts_ns: int
    value: float | int | bool | str | None
    severity: AlarmSeverity
    source: Literal["archive"] = "archive"


class ArchiverSeriesDTO(CanopyModel):
    """Raw-mode response for `GET /mole/v1/data/{pv}`.

    `from`/`to` carry the resolved window in epoch nanoseconds (handy when
    the request used `to=now`). `decimated` flags whether the service-side
    fallback decimator engaged.
    """

    mode: Literal["raw"] = "raw"
    pv: str
    archiver: str
    # `from` is a Python keyword — alias the wire name explicitly.
    from_ns: int = Field(alias="from")
    to_ns: int = Field(alias="to")
    decimated: bool
    samples: list[SampleDTO]


class BinnedPointDTO(CanopyModel):
    """Statistical bucket from a server-side binning operator."""

    ts_ns: int
    bin_ns: int
    mean: float
    std_dev: float
    min: float
    max: float
    count: int
    severity: AlarmSeverity


class ArchiverBinnedSeriesDTO(CanopyModel):
    """Binned-mode response for `GET /mole/v1/data/{pv}` when `binning` is set.

    The discriminator field is `mode` (always `"binned"` here). Bucket widths
    may vary in `auto` mode; `binSizeNs` is `null` there and `binNs` per point
    carries the actual gap. In `time` mode `binSizeNs` is set to the requested
    value.
    """

    mode: Literal["binned"] = "binned"
    pv: str
    archiver: str
    from_ns: int = Field(alias="from")
    to_ns: int = Field(alias="to")
    binning: Literal["auto", "time"]
    bin_size_ns: int | None = None
    target_buckets: int | None = None
    points: list[BinnedPointDTO]
