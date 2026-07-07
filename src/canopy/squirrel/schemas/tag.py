"""Tag and tag-group DTOs."""

from datetime import datetime

from pydantic import Field

from canopy.contracts.base import CanopyModel


class TagBase(CanopyModel):
    name: str = Field(..., min_length=1, max_length=255)
    description: str | None = None


class TagCreate(TagBase):
    pass


class TagUpdate(CanopyModel):
    name: str | None = Field(None, min_length=1, max_length=255)
    description: str | None = None


class TagDTO(TagBase):
    id: str


class TagGroupBase(CanopyModel):
    name: str = Field(..., min_length=1, max_length=255)
    description: str | None = None


class TagGroupCreate(TagGroupBase):
    pass


class TagGroupUpdate(CanopyModel):
    name: str | None = Field(None, min_length=1, max_length=255)
    description: str | None = None


class TagGroupSummaryDTO(CanopyModel):
    """Summary DTO without the full tag list."""

    id: str
    name: str
    description: str | None = None
    tag_count: int = 0


class TagGroupDTO(TagGroupBase):
    """Full tag group with its tags."""

    id: str
    tags: list[TagDTO] = []
    created_date: datetime
    last_modified_date: datetime


class AddTagResponse(CanopyModel):
    """Response from ``POST /v1/tags/{group_id}/tags``."""

    group: TagGroupDTO
    was_created: bool
