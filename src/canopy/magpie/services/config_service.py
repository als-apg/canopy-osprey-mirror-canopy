"""Config store domain service."""

from __future__ import annotations

import json
import secrets

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from canopy.core.errors import BadRequest
from canopy.magpie.errors import ConfigSlugConflict
from canopy.magpie.data.models import Config
from canopy.contracts.pagination import PagedResult
from canopy.magpie.schemas.config import (
    MAX_DATA_BYTES,
    ConfigDTO,
    NewConfigDTO,
    UpdateConfigDTO,
)
from canopy.magpie.data.repositories import ConfigRepository

# Unguessable public token. base62 avoids URL-encoding surprises; length 10 gives
# 62^10 ≈ 8.4e17 values, so the collision-retry loop below is just a safety net.
_SLUG_ALPHABET = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
_SLUG_LENGTH = 10
_SLUG_MAX_RETRIES = 5


def _new_slug() -> str:
    return "".join(secrets.choice(_SLUG_ALPHABET) for _ in range(_SLUG_LENGTH))


class ConfigService:
    """Config store CRUD. Owns no transaction — the caller commits.

    Every read/mutate is scoped by ``app``: the slug locates the row (slugs are
    globally unique), then the service asserts the row's ``app`` matches the
    expected one, treating a mismatch as "not found". This app handshake keeps a
    consumer from accidentally loading another app's config.
    """

    def __init__(self, session: AsyncSession):
        self.session = session
        self.repo = ConfigRepository(session)

    def _to_dto(self, config: Config) -> ConfigDTO:
        return ConfigDTO(
            id=config.id,
            app=config.app,
            slug=config.slug,
            name=config.name,
            description=config.description,
            data=config.data,
            created_by=config.created_by,
            created_date=config.created_at,
            last_modified_date=config.updated_at,
        )

    @staticmethod
    def _check_size(data: dict) -> None:
        if len(json.dumps(data).encode("utf-8")) > MAX_DATA_BYTES:
            raise BadRequest(f"Config data exceeds the {MAX_DATA_BYTES // 1024} KB limit")

    async def create(self, app: str, data: NewConfigDTO) -> ConfigDTO:
        self._check_size(data.data)
        # Retry on the (astronomically unlikely) slug collision; a fresh slug per attempt.
        for attempt in range(_SLUG_MAX_RETRIES):
            config = Config(
                slug=_new_slug(),
                app=app,
                name=data.name,
                description=data.description,
                data=data.data,
                created_by=data.created_by,
            )
            try:
                config = await self.repo.create(config)
            except IntegrityError:
                await self.session.rollback()
                if attempt == _SLUG_MAX_RETRIES - 1:
                    raise ConfigSlugConflict("Could not allocate a unique config slug") from None
                continue
            return self._to_dto(config)
        # Unreachable — the loop either returns or raises on the last attempt.
        raise ConfigSlugConflict("Could not allocate a unique config slug")

    async def _get_for_app(self, app: str, slug: str) -> Config | None:
        """Load by slug, enforcing the app handshake: a mismatch reads as absent."""
        config = await self.repo.get_by_slug(slug)
        if config is None or config.app != app:
            return None
        return config

    async def get_by_slug(self, app: str, slug: str) -> ConfigDTO | None:
        config = await self._get_for_app(app, slug)
        return self._to_dto(config) if config else None

    async def list_by_app(
        self,
        app: str,
        search: str | None = None,
        page_size: int = 100,
        continuation_token: str | None = None,
    ) -> PagedResult[ConfigDTO]:
        configs, next_token, total_count = await self.repo.list_by_app(
            app=app,
            search=search,
            limit=page_size,
            continuation_token=continuation_token,
        )
        return PagedResult(
            results=[self._to_dto(c) for c in configs],
            continuation_token=next_token,
            total_count=total_count,
        )

    async def update(self, app: str, slug: str, data: UpdateConfigDTO) -> ConfigDTO | None:
        config = await self._get_for_app(app, slug)
        if config is None:
            return None
        if data.data is not None:
            self._check_size(data.data)
            config.data = data.data
        if data.name is not None:
            config.name = data.name
        if data.description is not None:
            config.description = data.description
        config = await self.repo.update(config)
        return self._to_dto(config)

    async def delete(self, app: str, slug: str) -> bool:
        config = await self._get_for_app(app, slug)
        if config is None:
            return False
        await self.repo.delete(config)
        return True
