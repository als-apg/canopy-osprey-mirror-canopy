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
    # The connector binding the issuing screen was shown (the heartbeat's
    # ``binding``). A connector that can switch what it is connected to may
    # refuse a write naming another; the shipped backends ignore it.
    binding: str | None = None
    # The screen and revision that issued the write, for the connector's audit record.
    origin: PVWriteOrigin | None = None
    # True when a person confirmed this write in the viewer's dialog. A
    # connector whose write state is ``asked`` refuses a write without it; the
    # shipped backends ignore it.
    confirmed: bool = False


class PVWriteResultDTO(CanopyModel):
    """Result of a successful PV write (a non-2xx status signals failure)."""

    pv: str
