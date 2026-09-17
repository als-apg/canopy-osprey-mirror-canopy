"""API key DTOs. Field names mirror react-squirrel-backend's so SLAC clients see
one shape across both services (``readAccess`` on the wire, via CanopyModel)."""

from datetime import datetime

from canopy.contracts.base import CanopyModel


class ApiKeyDTO(CanopyModel):
    """A key's metadata. Never carries the token."""

    id: str
    app_name: str
    is_active: bool
    read_access: bool
    write_access: bool
    admin_access: bool
    created_at: datetime
    updated_at: datetime
    last_used_at: datetime | None = None


class ApiKeyCreateResultDTO(ApiKeyDTO):
    """Creation response — the only place a plaintext token is ever returned."""

    token: str


class NewApiKeyDTO(CanopyModel):
    """Request body for minting a key. At least one scope must be set."""

    app_name: str
    read: bool = False
    write: bool = False
    admin: bool = False
