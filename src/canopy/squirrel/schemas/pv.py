"""PV registry DTOs."""

from datetime import datetime

from pydantic import Field, model_validator

from canopy.contracts.base import CanopyModel
from canopy.squirrel.schemas.tag import TagDTO


class PVBase(CanopyModel):
    setpoint_address: str | None = Field(None, max_length=255)
    readback_address: str | None = Field(None, max_length=255)
    config_address: str | None = Field(None, max_length=255)
    device: str | None = Field(None, max_length=255)
    description: str | None = None
    abs_tolerance: float = 0.0
    rel_tolerance: float = 0.0


class NewPVElementDTO(PVBase):
    """DTO for creating a PV. At least one address is required."""

    tags: list[str] = []  # Tag IDs

    @model_validator(mode="after")
    def validate_at_least_one_address(self):
        if not any([self.setpoint_address, self.readback_address, self.config_address]):
            raise ValueError("At least one address (setpoint, readback, or config) is required")
        return self


class UpdatePVElementDTO(CanopyModel):
    """Partial update of a PV. Address fields are intentionally immutable here."""

    device: str | None = None
    description: str | None = None
    abs_tolerance: float | None = None
    rel_tolerance: float | None = None
    tags: list[str] | None = None  # Tag IDs


class PVElementDTO(PVBase):
    """Full PV response DTO."""

    id: str
    tags: list[TagDTO] = []
    created_date: datetime
    last_modified_date: datetime
    created_by: str | None = None
    last_modified_by: str | None = None


class PVSearchResponse(CanopyModel):
    """Response from ``GET /v1/pvs/search`` (offset paginated). The source's
    ``liveValues`` fields are dropped with the live-PV path."""

    results: list[PVElementDTO]
    total_count: int
    limit: int
    offset: int
