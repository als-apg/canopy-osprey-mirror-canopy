"""Shared async database foundation: engine, session, and ORM base/mixins.

Any module which needs persistence can reuse this engine and ``Base``. The
architecture is one Postgres with one schema per module — set per model via
``__table_args__ = {"schema": "<module>"}``. All modules' ORM metadata lives on
this single ``Base.metadata`` so the one Alembic tree (``migrations/``) can
autogenerate across modules.
"""

from __future__ import annotations

from uuid import uuid4
from datetime import datetime
from contextlib import asynccontextmanager
from collections.abc import AsyncGenerator

from sqlalchemy import DateTime, func, select
from sqlalchemy.orm import Mapped, DeclarativeBase, mapped_column
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.dialects.postgresql import UUID

from canopy.core.config import get_settings


class Base(DeclarativeBase):
    """Declarative base shared by every module's ORM models."""


class UUIDMixin:
    """String UUID primary key (``as_uuid=False`` keeps ids JSON/opaque-ref friendly)."""

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=lambda: str(uuid4()))


class TimestampMixin:
    """``created_at`` / ``updated_at`` managed by the database."""

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class BaseRepository[ModelType: Base]:
    """Base repository with common CRUD operations (flush-not-commit).

    Each repository takes an ``AsyncSession`` and only ever ``flush()``es — the
    transaction boundary (commit / rollback) is owned by the caller (``get_session``
    request dep, or ``session_scope`` in a worker task). Shared here so every
    persistent module reuses one definition.
    """

    def __init__(self, model: type[ModelType], session: AsyncSession):
        self.model = model
        self.session = session

    async def get_by_id(self, id: str) -> ModelType | None:
        return await self.session.get(self.model, id)

    async def get_all(self) -> list[ModelType]:
        result = await self.session.execute(select(self.model))
        return list(result.scalars().all())

    async def create(self, obj: ModelType) -> ModelType:
        self.session.add(obj)
        await self.session.flush()
        await self.session.refresh(obj)
        return obj

    async def update(self, obj: ModelType) -> ModelType:
        await self.session.flush()
        await self.session.refresh(obj)
        return obj

    async def delete(self, obj: ModelType) -> None:
        await self.session.delete(obj)
        await self.session.flush()

    async def count(self) -> int:
        result = await self.session.execute(select(func.count()).select_from(self.model))
        return result.scalar() or 0


_engine: AsyncEngine | None = None
_session_maker: async_sessionmaker[AsyncSession] | None = None


def get_engine() -> AsyncEngine:
    """Return the process-wide async engine, building it lazily on first use.

    Lazy construction keeps importing this module side-effect free (no
    ``DATABASE_URL`` required at import time) so unit tests that never touch
    Postgres can import models freely. The same engine serves both the
    ``canopy-api`` process and ``canopy-worker``.
    """
    global _engine, _session_maker
    if _engine is None:
        settings = get_settings()
        _engine = create_async_engine(
            settings.database_url,
            pool_size=settings.database_pool_size,
            max_overflow=settings.database_max_overflow,
            echo=settings.debug,
        )
        _session_maker = async_sessionmaker(_engine, class_=AsyncSession, expire_on_commit=False, autoflush=False)
    return _engine


def get_session_maker() -> async_sessionmaker[AsyncSession]:
    if _session_maker is None:
        get_engine()
    assert _session_maker is not None
    return _session_maker


@asynccontextmanager
async def session_scope() -> AsyncGenerator[AsyncSession, None]:
    """A session with explicit transaction control — it does NOT commit for you.

    The caller decides when (or whether) to commit, so a worker task can make a
    multi-step write all-or-nothing and an Arq retry (``max_tries``) never re-runs
    against partially-committed state. Rolls back if the block raises.
    """
    async with get_session_maker()() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI request dependency: a session that commits once on success and rolls
    back on error. Background/worker code that needs its own transaction boundaries
    should use ``session_scope`` instead.
    """
    async with session_scope() as session:
        yield session
        await session.commit()


async def dispose_engine() -> None:
    """Dispose the engine on shutdown. Call from the app / worker lifespan."""
    global _engine, _session_maker
    if _engine is not None:
        await _engine.dispose()
        _engine = None
        _session_maker = None
