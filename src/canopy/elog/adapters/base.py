"""Adapter contract for e-log backends.

`ElogAdapter` is a Protocol; the in-tree `ElogPlusAdapter` implements it and
other labs' backends can too. One adapter instance serves one client app: it
holds that app's token and anything cached about that token's identity.

`register_elog_provider` registers a short provider name; the `module:Class`
dotted-path form in ``CANOPY_ELOG__PROVIDER`` is resolved at registry-build
time without needing prior registration.

Adapters raise the `canopy.elog.errors` types: `ElogUpstreamError` /
`ElogUpstreamTimeout` for backend failures, `ElogNotSupported` for an
operation the backend has no equivalent of.
"""

from typing import Protocol, runtime_checkable

from canopy.elog.config import ElogConfig, ElogAppConfig
from canopy.contracts.base import CanopyModel
from canopy.elog.schemas.elog import (
    ElogTagDTO,
    ElogUserDTO,
    ElogLogbookDTO,
    ElogAttachmentDTO,
    ElogEntryCreateDTO,
    ElogEntryResultDTO,
    ElogRecentEntryDTO,
)


class AdapterHealth(CanopyModel):
    healthy: bool
    identity: str | None = None
    latency_ms: float | None = None
    last_error: str | None = None


@runtime_checkable
class ElogAdapter(Protocol):
    """Contract every e-log adapter must satisfy.

    ``app`` is the client app this instance posts for; ``provider_name`` is the
    registered short name (or dotted path) it was built from.
    """

    app: str
    provider_name: str

    @classmethod
    def from_config(cls, app: str, app_config: ElogAppConfig, config: ElogConfig) -> "ElogAdapter":
        """Build the adapter for one app. Raise `ElogNotConfigured` if provider config is missing."""
        ...

    async def list_logbooks(self) -> list[ElogLogbookDTO]: ...

    async def list_tags(self, logbook_id: str) -> list[ElogTagDTO]: ...

    async def search_users(self, search: str, limit: int) -> list[ElogUserDTO]: ...

    async def list_recent_entries(self, logbook_ids: list[str], limit: int) -> list[ElogRecentEntryDTO]:
        """Most recent entries posted by this adapter's own token, newest first."""
        ...

    async def upload_attachment(self, filename: str, content_type: str | None, data: bytes) -> ElogAttachmentDTO: ...

    async def create_entry(self, entry: ElogEntryCreateDTO) -> ElogEntryResultDTO:
        """Post a new entry. ``entry.follows_up_entry_id`` is ignored here."""
        ...

    async def create_follow_up(self, parent_entry_id: str, entry: ElogEntryCreateDTO) -> ElogEntryResultDTO: ...

    async def health(self) -> AdapterHealth:
        """Cheap identity round trip with this app's token."""
        ...

    async def close(self) -> None:
        """Release the underlying client. Called from the lifespan shutdown."""
        ...


# --------------------------------------------------------------------------- #
# Provider registration: short-name -> class. Out-of-tree adapters can skip
# this and use the `module:Class` dotted-path form in config instead.
# --------------------------------------------------------------------------- #

_REGISTERED_PROVIDERS: dict[str, type] = {}


def register_elog_provider(name: str):
    """Decorator that registers an adapter class under a short provider name.

    Example::

        @register_elog_provider("elog_plus")
        class ElogPlusAdapter:
            ...

    Then ``CANOPY_ELOG__PROVIDER=elog_plus`` resolves to this class.
    """

    def deco(cls: type) -> type:
        cls.provider_name = name
        _REGISTERED_PROVIDERS[name] = cls
        return cls

    return deco


def get_registered_provider(name: str) -> type | None:
    return _REGISTERED_PROVIDERS.get(name)
