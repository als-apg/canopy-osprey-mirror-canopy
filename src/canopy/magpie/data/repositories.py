"""Magpie repositories — thin data-access over ``core.db`` sessions.

Reuses the shared ``BaseRepository`` (flush-not-commit) from ``canopy.core.db``;
the transaction boundary is owned by the caller (``get_session`` request dep, or
``session_scope``).
"""

from __future__ import annotations

from sqlalchemy import or_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from canopy.core.db import BaseRepository
from canopy.magpie.data.models import Config


class ConfigRepository(BaseRepository[Config]):
    """Repository for Config operations."""

    def __init__(self, session: AsyncSession):
        super().__init__(Config, session)

    async def get_by_slug(self, slug: str) -> Config | None:
        result = await self.session.execute(select(Config).where(Config.slug == slug))
        return result.scalar_one_or_none()

    async def list_by_app(
        self,
        app: str,
        search: str | None = None,
        limit: int = 100,
        continuation_token: str | None = None,
    ) -> tuple[list[Config], str | None, int]:
        """Keyset-paginated list of an app's configs. Returns ``(results, next_token, total_count)``.

        Ordered by ``id`` so the continuation token is a stable cursor; ``search`` does a
        case-insensitive contains over name/description. Mirrors squirrel's
        ``PvRepository.search_by_name``.
        """
        query = select(Config).where(Config.app == app)
        count_query = select(func.count()).select_from(Config).where(Config.app == app)

        if search:
            search_filter = or_(
                Config.name.ilike(f"%{search}%"),
                Config.description.ilike(f"%{search}%"),
            )
            query = query.where(search_filter)
            count_query = count_query.where(search_filter)

        if continuation_token:
            query = query.where(Config.id > continuation_token)

        query = query.order_by(Config.id).limit(limit + 1)  # +1 to detect a next page

        result = await self.session.execute(query)
        configs = list(result.scalars().all())

        count_result = await self.session.execute(count_query)
        total_count = count_result.scalar() or 0

        next_token = None
        if len(configs) > limit:
            configs = configs[:limit]
            next_token = configs[-1].id

        return configs, next_token, total_count

    async def list_apps(self) -> list[tuple[str, int]]:
        """``(app, count)`` for every app that has configs, ordered by app name.

        There is no separate app registry — an app exists only by having rows here.
        """
        result = await self.session.execute(select(Config.app, func.count()).group_by(Config.app).order_by(Config.app))
        return [(app, count) for app, count in result.all()]

    async def list_all(
        self,
        search: str | None = None,
        limit: int = 100,
        continuation_token: str | None = None,
    ) -> tuple[list[Config], str | None, int]:
        """Cross-app keyset list — ``list_by_app`` without the ``app`` filter.

        Same ``id`` cursor, ``limit + 1`` next-page detection, and search filter, so a
        continuation token from here is interchangeable in shape with the per-app one.
        """
        query = select(Config)
        count_query = select(func.count()).select_from(Config)

        if search:
            search_filter = or_(
                Config.name.ilike(f"%{search}%"),
                Config.description.ilike(f"%{search}%"),
            )
            query = query.where(search_filter)
            count_query = count_query.where(search_filter)

        if continuation_token:
            query = query.where(Config.id > continuation_token)

        query = query.order_by(Config.id).limit(limit + 1)  # +1 to detect a next page

        result = await self.session.execute(query)
        configs = list(result.scalars().all())

        count_result = await self.session.execute(count_query)
        total_count = count_result.scalar() or 0

        next_token = None
        if len(configs) > limit:
            configs = configs[:limit]
            next_token = configs[-1].id

        return configs, next_token, total_count
