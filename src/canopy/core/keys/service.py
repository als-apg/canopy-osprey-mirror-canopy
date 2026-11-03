"""API key lifecycle: mint, list, deactivate.

Used by the provisioning CLI scripts today. Deliberately separate from the
verification path (``canopy.core.keys.registry``), which is latency-sensitive and
caches; this side runs once per operator action.
"""

from __future__ import annotations

import logging
from uuid import UUID

from canopy.core.errors import (
    BadRequest,
    ApiKeyNotFound,
    ApiKeySelfDeactivation,
    InsufficientModuleAccess,
    InsufficientFacilityAccess,
)
from canopy.core.modules import MODULES, WILDCARD, split_modules
from canopy.core.security import invalidate_key_id
from canopy.core.facilities import gateway as facilities_gateway
from canopy.core.keys.models import ApiKey
from canopy.core.keys.tokens import hash_token, mint_token
from canopy.core.keys.schemas import ApiKeyDTO, ApiKeyCreateResultDTO
from canopy.core.keys.repositories import ApiKeyRepository

logger = logging.getLogger(__name__)


def to_dto(api_key: ApiKey) -> ApiKeyDTO:
    return ApiKeyDTO(
        id=api_key.id,
        app_name=api_key.app_name,
        is_active=api_key.is_active,
        read_access=api_key.read_access,
        write_access=api_key.write_access,
        admin_access=api_key.admin_access,
        modules=api_key.modules,
        facilities=api_key.facilities,
        created_at=api_key.created_at,
        updated_at=api_key.updated_at,
        last_used_at=api_key.last_used_at,
    )


def _normalize_modules(modules: list[str]) -> list[str]:
    """Validate and canonicalize a requested module list.

    Callers must pass ``[WILDCARD]`` explicitly for "every module" — there is no
    implicit default here. An empty list is rejected the same way an
    all-scopes-false key is: it isn't a credential, it's a mistake that will be
    diagnosed as an outage.
    """
    cleaned = {m.strip() for m in modules if m.strip()}
    if not cleaned:
        raise BadRequest(f"Cannot create an API key scoped to no modules; pass at least one, or '{WILDCARD}' for all")
    known, unknown = split_modules(cleaned)
    if unknown:
        raise BadRequest(f"Unknown module(s) {sorted(unknown)}; valid modules are {sorted(MODULES)}")
    return sorted(known)


def _require_module_containment(target_modules, acting_modules) -> None:
    """Refuse an admin action that reaches outside the acting key's own modules.

    ``acting_modules`` is ``None`` for the CLI scripts, which mint/deactivate
    straight against the database with no acting key to contain against —
    that path stays unrestricted, same as before this check existed. Over
    HTTP an admin key is only ever a delegated admin for its own modules
    unless it holds ``WILDCARD``, so ``ops:admin@core`` cannot mint or
    deactivate a key scoped to ``squirrel``.
    """
    if acting_modules is None or WILDCARD in acting_modules:
        return
    if outside := set(target_modules) - set(acting_modules):
        raise InsufficientModuleAccess(f"Acting API key is not scoped to module(s) {sorted(outside)}")


async def _require_facility_containment(target_facilities: list[str], acting_key_facilities: list[str] | None) -> None:
    """Refuse an admin action that reaches outside the acting key's own facility grant.

    Mirrors ``_require_module_containment``, but facility containment needs hierarchy
    expansion — a grant on a parent covers a child the target list names directly —
    so each target slug is checked by resolving its descendant set and testing it
    against the acting key's own resolved scope, rather than comparing slug sets
    directly. ``acting_key_facilities`` is ``None`` for the CLI, which stays
    unrestricted, same as module containment.

    An acting key not holding the wildcard can never delegate the wildcard: there is
    no finite resolved scope to compare it against, since a future facility would
    fall inside it too.
    """
    if acting_key_facilities is None or WILDCARD in acting_key_facilities:
        return
    if WILDCARD in target_facilities:
        raise InsufficientFacilityAccess("Acting API key is not scoped to every facility and cannot delegate '*'")
    acting_scope = await facilities_gateway.resolve_key_facility_scope(frozenset(acting_key_facilities))
    outside = set()
    for slug in target_facilities:
        slug_scope = await facilities_gateway.resolve_key_facility_scope(frozenset({slug}))
        if slug_scope and not slug_scope <= acting_scope:
            outside.add(slug)
    if outside:
        raise InsufficientFacilityAccess(f"Acting API key is not scoped to facility/facilities {sorted(outside)}")


def _normalize_facilities(facilities: list[str]) -> list[str]:
    """Canonicalize a requested facility list.

    Unlike ``_normalize_modules``, an empty list is valid and meaningful —
    "provisioned but not yet assigned to a facility" / "data-suspended without
    revoking the credential" — so it is returned as-is rather than rejected.

    Whether the named slugs *exist* is checked separately, in ``create_key``,
    where there's a session in hand; this stays a pure string transform.
    """
    cleaned = {f.strip() for f in facilities if f.strip()}
    if WILDCARD in cleaned:
        return [WILDCARD]
    return sorted(cleaned)


class ApiKeyService:
    """Create and manage API keys."""

    def __init__(self, session):
        self.session = session
        self.repo = ApiKeyRepository(session)

    async def create_key(
        self,
        app_name: str,
        *,
        read: bool = False,
        write: bool = False,
        admin: bool = False,
        modules: list[str],
        acting_key_modules: list[str] | None = None,
        facilities: list[str] | None = None,
        acting_key_facilities: list[str] | None = None,
    ) -> ApiKeyCreateResultDTO:
        """Mint a key and return it with its plaintext token — the only time the
        token exists outside the caller's hands. Only its hash is stored.

        An existing active key for the same ``app_name`` is *not* an error: that
        overlap is how a rotation works.

        ``modules`` restricts which mounted sub-apps (or ``core``, for key
        management itself) the key may call. Pass ``[WILDCARD]`` for every module
        — callers must choose explicitly, there is no implicit default here.

        ``acting_key_modules`` is the module set of the admin key making this
        request (``None`` for the CLI). A module-scoped admin can only delegate
        modules it itself holds — it cannot mint a key scoped wider than itself.

        ``facilities`` defaults to no grants (``[]``) rather than every module's
        implicit ``[WILDCARD]`` — unlike modules, an unassigned key is a valid,
        unprivileged state here, not a mistake.

        ``acting_key_facilities`` (``None`` for the CLI) is the same delegation check
        as ``acting_key_modules``, for facility scope: a facility-scoped admin cannot
        mint a key granted a facility outside its own resolved scope, or the wildcard.
        """
        app_name = app_name.strip()
        if not app_name:
            raise BadRequest("app_name must not be empty")
        if not (read or write or admin):
            raise BadRequest("Cannot create an API key with no access; pass at least one scope")
        module_list = _normalize_modules(modules)
        _require_module_containment(module_list, acting_key_modules)
        facility_list = _normalize_facilities(facilities or [])
        # A grant naming a nonexistent facility is a hard reject: it resolves to
        # "matches nothing" at request time, so the key would authenticate and then
        # see and write nothing, with no error to point at the typo. `[WILDCARD]`
        # names no slug, so there's nothing to check.
        if facility_list and facility_list != [WILDCARD]:
            unknown = set(facility_list) - await facilities_gateway.known_slugs(facility_list)
            if unknown:
                raise BadRequest(
                    f"Unknown facility slug(s) {sorted(unknown)}; create them first "
                    f"(scripts/create_facility.py or POST /v1/facilities)"
                )
        await _require_facility_containment(facility_list, acting_key_facilities)

        token = mint_token()
        created = await self.repo.create(
            ApiKey(
                app_name=app_name,
                token_hash=hash_token(token),
                read_access=read,
                write_access=write,
                admin_access=admin,
                modules=module_list,
                facilities=facility_list,
            )
        )
        # The token itself is never logged — app_name and id are the audit trail.
        logger.info("Created API key id=%s app_name=%s", created.id, created.app_name)
        return ApiKeyCreateResultDTO(token=token, **to_dto(created).model_dump())

    async def list_keys(self, active_only: bool = False, app_name: str | None = None) -> list[ApiKeyDTO]:
        if app_name is not None:
            keys = await self.repo.get_by_app_name(app_name, active_only)
        else:
            keys = await self.repo.get_all(active_only)
        return [to_dto(key) for key in keys]

    async def _find(self, key_id: str) -> ApiKey | None:
        """Look a key up by id, treating a malformed id as simply absent.

        The id column is a UUID, so handing asyncpg a mistyped id raises a DataError
        — a 500 through a route, and a wall of traceback from the CLI. Validate first
        and report it the way a wrong-but-well-formed id is reported.
        """
        try:
            UUID(key_id)
        except ValueError:
            return None
        return await self.repo.get_by_id(key_id)

    async def get_by_id(self, key_id: str) -> ApiKeyDTO | None:
        api_key = await self._find(key_id)
        return to_dto(api_key) if api_key else None

    async def count(self, active_only: bool = False) -> int:
        return await self.repo.count(active_only)

    async def deactivate_key(
        self,
        key_id: str,
        *,
        acting_key_id: str | None = None,
        acting_key_modules: list[str] | None = None,
        force: bool = False,
        acting_key_facilities: list[str] | None = None,
    ) -> ApiKeyDTO:
        """Soft-delete a key. Idempotent-adjacent: deactivating an already inactive
        key is a no-op rather than an error, so a retried revocation still succeeds.

        ``acting_key_id`` is the id of the key that authenticated this call (``None``
        for the CLI, which has no HTTP request to guard). Deactivating the key you're
        making the request with would lock an operator out mid-session, so it's
        refused unless ``force`` is set.

        ``acting_key_modules`` (``None`` for the CLI) contains the same delegation
        check as ``create_key``: a module-scoped admin cannot deactivate a key
        scoped to a module it does not itself hold. ``acting_key_facilities`` is the
        same check for facility scope.
        """
        api_key = await self._find(key_id)
        if api_key is None:
            raise ApiKeyNotFound(f"No API key with id '{key_id}'")
        _require_module_containment(api_key.modules, acting_key_modules)
        await _require_facility_containment(api_key.facilities, acting_key_facilities)
        if not force and acting_key_id is not None and api_key.id == acting_key_id:
            raise ApiKeySelfDeactivation(
                "Refusing to deactivate the API key authenticating this request; pass force=true to override"
            )
        if not api_key.is_active:
            return to_dto(api_key)
        deactivated = await self.repo.deactivate(api_key)
        invalidate_key_id(api_key.id)
        logger.info("Deactivated API key id=%s app_name=%s", api_key.id, api_key.app_name)
        return to_dto(deactivated)
