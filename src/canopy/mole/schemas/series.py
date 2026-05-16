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
