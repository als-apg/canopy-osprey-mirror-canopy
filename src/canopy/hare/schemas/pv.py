"""Request/response DTOs for the Hare PV REST endpoints."""

from pydantic import Field

from canopy.contracts.pv import PVWriteValue
from canopy.contracts.base import CanopyModel


class PVWriteOrigin(CanopyModel):
    """Where a write came from: the screen that issued it and its revision (audit only)."""

    screen: str
    rev: str | None = None


class PVWriteDTO(CanopyModel):
    """Body of ``PUT /hare/v1/pvs/{pv}`` — the value to write, plus write options."""

    value: PVWriteValue
    # Block until the IOC processes the write (put-with-callback) so a 2xx means
    # the value landed. False submits without waiting for record processing.
    wait: bool = True
    # Per-write timeout in seconds (must be positive); None uses the connector's default.
    timeout: float | None = Field(default=None, gt=0)
    # The control-target binding the screen showed (WC6 binding rule). Generic
    # canopy ignores it; a backend that checks it refuses a PUT naming another.
    binding: str | None = None
    # The screen and revision that issued the write, for the write's audit record.
    origin: PVWriteOrigin | None = None


class PVWriteResultDTO(CanopyModel):
    """Result of a successful PV write (a non-2xx status signals failure)."""

    pv: str
