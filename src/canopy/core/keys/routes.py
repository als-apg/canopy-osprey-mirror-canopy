"""HTTP surface for API key management — mint, list, count, deactivate.

Cross-module (owned by no sub-app), so it lives on the parent app at
``/v1/api-keys`` rather than inside a mount. Every route gates on ``admin``,
never ``write``: ``write`` is held by every client that can set a live
setpoint — the largest, least-controlled population of keys — and letting one
mint credentials that outlive its own rotation is the wrong default.
"""

from fastapi import Query, Security, APIRouter

from canopy.core.security import AdminKeyDep, require_admin_access
from canopy.core.keys.schemas import ApiKeyDTO, NewApiKeyDTO, ApiKeyCreateResultDTO
from canopy.core.keys.dependencies import ApiKeyServiceDep

router = APIRouter(prefix="/v1/api-keys", tags=["API Keys"], dependencies=[Security(require_admin_access)])


@router.post("")
async def create_key(
    data: NewApiKeyDTO,
    key: AdminKeyDep,  # reuses the router-level Security() call, doesn't re-resolve the token
    service: ApiKeyServiceDep,
) -> ApiKeyCreateResultDTO:
    """Mint a key. The plaintext token is returned here, once, and never again."""
    return await service.create_key(
        data.app_name,
        read=data.read,
        write=data.write,
        admin=data.admin,
        modules=data.modules,
        acting_key_modules=list(key.modules),
    )


@router.get("")
async def list_keys(
    service: ApiKeyServiceDep,
    activeOnly: bool = Query(False),
    appName: str | None = Query(None, description="Filter to keys for a single app — the overlap during a rotation"),
) -> list[ApiKeyDTO]:
    return await service.list_keys(active_only=activeOnly, app_name=appName)


@router.get("/count")
async def count_keys(service: ApiKeyServiceDep, activeOnly: bool = Query(False)) -> int:
    return await service.count(active_only=activeOnly)


@router.delete("/{key_id}")
async def deactivate_key(
    key_id: str,
    key: AdminKeyDep,  # reuses the router-level Security() call, doesn't re-resolve the token
    service: ApiKeyServiceDep,
    force: bool = Query(False, description="Deactivate even if this is the key authenticating the request"),
) -> ApiKeyDTO:
    return await service.deactivate_key(key_id, acting_key_id=key.id, acting_key_modules=list(key.modules), force=force)
