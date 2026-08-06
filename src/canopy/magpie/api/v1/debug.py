"""Operational/diagnostic routes that deliberately pierce the per-app boundary.

The consumer API is app-scoped: you reach a config through ``/configs/{app}/...``
and the service 404s any row whose ``app`` doesn't match. These routes intentionally
ignore that handshake to answer operator questions — "show me everything" and "which
app owns this slug?" — so they live under ``/_debug`` (mirroring vole's
``/vole/v1/_debug/buffers``) to signal they are not part of the stable consumer
contract. Read scope only; they never mutate.
"""

from fastapi import Query, Security, APIRouter

from canopy.core.errors import BadRequest
from canopy.core.security import require_read_access
from canopy.magpie.errors import ConfigNotFound
from canopy.magpie.dependencies import ConfigServiceDep
from canopy.contracts.pagination import PagedResult
from canopy.magpie.schemas.config import ConfigDTO
from canopy.magpie.api.v1._validation import is_slug, is_uuid

router = APIRouter(prefix="/_debug", tags=["Magpie / Debug"])


@router.get("/configs", dependencies=[Security(require_read_access)])
async def list_all_configs(
    service: ConfigServiceDep,
    search: str | None = Query(None, max_length=255, description="Case-insensitive contains over name/description"),
    pageSize: int = Query(100, ge=1, le=1000),
    continuationToken: str | None = Query(None),
) -> PagedResult[ConfigDTO]:
    """Every config across all apps (keyset-paginated). Not app-scoped — debug only."""
    # Same token guard as the app-scoped list: a malformed one would reach asyncpg as a
    # bad UUID comparison and surface as a 500 rather than a 400.
    if continuationToken is not None and not is_uuid(continuationToken):
        raise BadRequest("Invalid continuationToken")
    return await service.list_all(
        search=search,
        page_size=pageSize,
        continuation_token=continuationToken,
    )


@router.get("/slugs/{slug}", dependencies=[Security(require_read_access)])
async def resolve_slug(slug: str, service: ConfigServiceDep) -> ConfigDTO:
    """Resolve a config by slug alone, ignoring the app handshake ("who owns this?")."""
    if not is_slug(slug):
        raise ConfigNotFound(f"Config '{slug}' not found")
    config = await service.resolve_slug(slug)
    if config is None:
        raise ConfigNotFound(f"Config '{slug}' not found")
    return config
