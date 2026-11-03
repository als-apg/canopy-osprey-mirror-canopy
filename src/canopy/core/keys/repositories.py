"""API key repository — thin data access over a ``core.db`` session.

Only ever ``flush()``es; the transaction boundary belongs to the caller
(``get_session`` request dep, or ``session_scope`` elsewhere), matching every
other repository in the codebase.
"""

from __future__ import annotations

from sqlalchemy import func, select, update

from canopy.core.db import BaseRepository
from canopy.core.keys.models import ApiKey


class ApiKeyRepository(BaseRepository[ApiKey]):
    """Repository for API key lookups and lifecycle."""

    def __init__(self, session):
        super().__init__(ApiKey, session)

    async def get_by_token_hash(self, token_hash: str, active_only: bool = True) -> ApiKey | None:
        """Resolve a key by token hash. Defaults to active keys, so a deactivated
        key reads as "no such key" on the auth path."""
        query = select(ApiKey).where(ApiKey.token_hash == token_hash)
        if active_only:
            query = query.where(ApiKey.is_active)
        result = await self.session.execute(query)
        return result.scalars().first()

    async def get_by_app_name(self, app_name: str, active_only: bool = True) -> list[ApiKey]:
        """Every key for an app name — a list, not one row: app names are not
        unique, which is what allows two keys to overlap during a rotation."""
        query = select(ApiKey).where(ApiKey.app_name == app_name)
        if active_only:
            query = query.where(ApiKey.is_active)
        result = await self.session.execute(query)
        return list(result.scalars().all())

    async def get_all(self, active_only: bool = False) -> list[ApiKey]:
        query = select(ApiKey).order_by(ApiKey.created_at)
        if active_only:
            query = query.where(ApiKey.is_active)
        result = await self.session.execute(query)
        return list(result.scalars().all())

    async def count(self, active_only: bool = False) -> int:
        query = select(func.count()).select_from(ApiKey)
        if active_only:
            query = query.where(ApiKey.is_active)
        result = await self.session.execute(query)
        return result.scalar() or 0

    async def count_granting(self, slug: str) -> int:
        """How many *active* keys grant this facility slug.

        Consulted before a facility is deleted: since a grant stores the slug, not
        an id, deleting the facility would silently turn every key holding it into
        a deny-all credential that still authenticates. Inactive keys are excluded
        — a revoked key must not pin a facility in place forever.
        """
        result = await self.session.execute(
            select(func.count()).select_from(ApiKey).where(ApiKey.facilities.any(slug), ApiKey.is_active)
        )
        return result.scalar() or 0

    async def deactivate(self, api_key: ApiKey) -> ApiKey:
        api_key.is_active = False
        return await self.update(api_key)

    async def touch_last_used(self, key_id: str) -> None:
        """Stamp ``last_used_at``. Issued as a bare UPDATE rather than loading and
        mutating the row, so it never fights the caller's identity map."""
        await self.session.execute(update(ApiKey).where(ApiKey.id == key_id).values(last_used_at=func.now()))
