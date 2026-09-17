"""API key DTOs. Field names mirror react-squirrel-backend's so SLAC clients see
one shape across both services (``readAccess`` on the wire, via CanopyModel)."""

from datetime import datetime

from pydantic import Field

from canopy.core.modules import WILDCARD
from canopy.contracts.base import CanopyModel


class ApiKeyDTO(CanopyModel):
    """A key's metadata. Never carries the token."""

    id: str
    app_name: str
    is_active: bool
    read_access: bool
    write_access: bool
    admin_access: bool
    modules: list[str]
    created_at: datetime
    updated_at: datetime
    last_used_at: datetime | None = None


class ApiKeyCreateResultDTO(ApiKeyDTO):
    """Creation response — the only place a plaintext token is ever returned."""

    token: str


class NewApiKeyDTO(CanopyModel):
    """Request body for minting a key. At least one scope must be set.

    ``modules`` defaults to ``[WILDCARD]`` — every module — but that's an
    explicit choice a caller is making, not an implicit fallback for omission.
    """

    app_name: str = Field(min_length=1, max_length=255)
    read: bool = False
    write: bool = False
    admin: bool = False
    modules: list[str] = [WILDCARD]
