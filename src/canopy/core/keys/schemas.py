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
    facilities: list[str]
    created_at: datetime
    updated_at: datetime
    last_used_at: datetime | None = None


class ApiKeyCreateResultDTO(ApiKeyDTO):
    """Creation response — the only place a plaintext token is ever returned."""

    token: str


class NewApiKeyDTO(CanopyModel):
    """Request body for minting a key. At least one scope must be set.

    ``modules`` defaults to ``[WILDCARD]`` — every module — matching
    ``scripts/create_key.py``'s CLI default. Deliberately permissive here,
    at the two entry points a human operator drives directly: the request
    still runs through ``_normalize_modules``/``_require_module_containment``
    in ``canopy.core.keys.service``, which reject an empty list and cap a
    module-scoped admin's mint to its own modules regardless of what a
    caller passes or omits.

    ``facilities`` defaults to ``[]`` — no grants yet — which is itself a valid
    state, unlike an empty ``modules`` list, which is rejected.
    """

    app_name: str = Field(min_length=1, max_length=255)
    read: bool = False
    write: bool = False
    admin: bool = False
    modules: list[str] = [WILDCARD]
    facilities: list[str] = []
