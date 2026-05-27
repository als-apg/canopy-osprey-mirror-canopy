from enum import StrEnum
from typing import Literal

from canopy.contracts.base import CanopyModel


class AlarmSeverity(StrEnum):
    NO_ALARM = "NO_ALARM"
    MINOR = "MINOR"
    MAJOR = "MAJOR"
    INVALID = "INVALID"


class Sample(CanopyModel):
    """A single archived or live sample of a PV."""

    ts_ns: int
    value: float | int | bool | str | bytes | None
    severity: AlarmSeverity
    source: Literal["live", "archive"]


class BinnedPoint(CanopyModel):
    """A statistical bucket from a server-side binning operator.

    Distinct from `Sample`: there is no single "value", instead the bucket
    summarizes the underlying samples in `[ts_ns, ts_ns + bin_ns)`.
    """

    ts_ns: int
    bin_ns: int
    mean: float
    std_dev: float
    min: float
    max: float
    count: int
    severity: AlarmSeverity
