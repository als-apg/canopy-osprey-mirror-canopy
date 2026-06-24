"""Request/response DTOs for the Hare PV REST endpoints."""

from pydantic import Field

from canopy.contracts.base import CanopyModel
from canopy.contracts.pv import PVWriteValue


class PVWriteDTO(CanopyModel):
    """Body of ``PUT /hare/v1/pvs/{pv}`` — the value to write, plus write options."""

    value: PVWriteValue
    # Block until the IOC processes the write (put-with-callback) so a 2xx means
    # the value landed. False submits without waiting for record processing.
    wait: bool = True
    # Per-write timeout in seconds (must be positive); None uses the connector's default.
    timeout: float | None = Field(default=None, gt=0)


class PVWriteResultDTO(CanopyModel):
    """Result of a successful PV write (a non-2xx status signals failure)."""

    pv: str
