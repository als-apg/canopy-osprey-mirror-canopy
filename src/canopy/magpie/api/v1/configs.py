"""Config store endpoints.

Endpoints are app-scoped in the path (``/configs/{app}/...``). Since slugs are
globally unique, ``app`` is not needed to *find* a config — it's an assertion: the
service treats a config whose ``app`` doesn't match the path as "not found". This
handshake stops a consumer from accidentally loading another app's config. It is a
correctness guard, not a security boundary (any valid key can read any app by
naming it). Auth is the standard ``X-API-Key`` read/write scopes.
"""

from fastapi import Query, Security, APIRouter

from canopy.core.errors import BadRequest
from canopy.core.security import require_read_access, require_write_access
from canopy.magpie.errors import ConfigNotFound
from canopy.magpie.dependencies import ConfigServiceDep
from canopy.contracts.pagination import PagedResult
from canopy.magpie.schemas.config import ConfigDTO, NewConfigDTO, UpdateConfigDTO
from canopy.magpie.api.v1._validation import is_app, is_slug, is_uuid

router = APIRouter(prefix="/configs", tags=["Configs"])


def _require_app(app: str) -> None:
    if not is_app(app):
        raise BadRequest(f"Invalid app name '{app}'")


@router.post("/{app}", dependencies=[Security(require_write_access)])
async def create_config(app: str, data: NewConfigDTO, service: ConfigServiceDep) -> ConfigDTO:
    """Create a config under ``app``."""
    _require_app(app)
    return await service.create(app, data)


@router.get("/{app}", dependencies=[Security(require_read_access)])
async def list_configs(
    app: str,
    service: ConfigServiceDep,
    search: str | None = Query(None, description="Case-insensitive contains over name/description"),
    pageSize: int = Query(100, ge=1, le=1000),
    continuationToken: str | None = Query(None),
) -> PagedResult[ConfigDTO]:
    """List an app's configs (keyset-paginated)."""
    _require_app(app)
    # The token is a row id; letting a malformed one through would reach asyncpg as a
    # bad UUID comparison and surface as a 500 rather than a 400.
    if continuationToken is not None and not is_uuid(continuationToken):
        raise BadRequest("Invalid continuationToken")
    return await service.list_by_app(
        app=app,
        search=search,
        page_size=pageSize,
        continuation_token=continuationToken,
    )


@router.get("/{app}/{slug}", dependencies=[Security(require_read_access)])
async def get_config(app: str, slug: str, service: ConfigServiceDep) -> ConfigDTO:
    """Resolve a config by slug (the shareable-link endpoint)."""
    if not is_slug(slug):
        raise ConfigNotFound(f"Config '{slug}' not found")
    config = await service.get_by_slug(app, slug)
    if config is None:
        raise ConfigNotFound(f"Config '{slug}' not found")
    return config


@router.put("/{app}/{slug}", dependencies=[Security(require_write_access)])
async def update_config(app: str, slug: str, data: UpdateConfigDTO, service: ConfigServiceDep) -> ConfigDTO:
    """Update a config's name/description/data (app and slug are immutable)."""
    if not is_slug(slug):
        raise ConfigNotFound(f"Config '{slug}' not found")
    config = await service.update(app, slug, data)
    if config is None:
        raise ConfigNotFound(f"Config '{slug}' not found")
    return config


@router.delete("/{app}/{slug}", dependencies=[Security(require_write_access)])
async def delete_config(app: str, slug: str, service: ConfigServiceDep) -> bool:
    """Delete a config."""
    if not is_slug(slug):
        raise ConfigNotFound(f"Config '{slug}' not found")
    if not await service.delete(app, slug):
        raise ConfigNotFound(f"Config '{slug}' not found")
    return True
