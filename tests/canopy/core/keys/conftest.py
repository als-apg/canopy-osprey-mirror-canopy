"""Fixtures for the persistent API key store.

A real Postgres *test database* (``canopy_test``, auto-created), with only the
``core`` schema built and dropped per test. Skips rather than fails when Postgres
is unreachable, so the repo's no-infra ``pytest`` run still passes; these light up
locally against canopy-postgres and in CI against the Postgres service.

``DbKeyRegistry`` deliberately opens its own session via ``core.db.session_scope``
rather than taking a request-scoped one, so it is pointed at the test database by
swapping the session maker on the ``core.db`` module — the same seam
``tests/canopy/core/test_db.py`` uses.
"""

import os
from contextlib import contextmanager

import pytest
import asyncpg
import pytest_asyncio
from fastapi import FastAPI, Security
from sqlalchemy import text
from sqlalchemy.pool import NullPool
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import canopy.core.keys.models  # noqa: F401 — register core tables on Base.metadata
from canopy.core import db, security
from canopy.core.db import Base
from canopy.core.errors import register_canopy_error_handler
from canopy.core.security import (
    APIKey,
    EnvKeyRegistry,
    bind_key_registry,
    require_read_access,
    require_write_access,
)
from canopy.core.keys.routes import router as api_keys_router
from canopy.core.keys.registry import (
    DbKeyRegistry,
    CachingKeyRegistry,
    ChainedKeyRegistry,
)

TEST_DATABASE_URL = os.environ.get(
    "CANOPY_TEST_DATABASE_URL", "postgresql+asyncpg://canopy:canopy@localhost:5432/canopy_test"
)
_ADMIN_DSN = os.environ.get("CANOPY_TEST_ADMIN_DSN", "postgresql://canopy:canopy@localhost:5432/postgres")
_TEST_DB_NAME = TEST_DATABASE_URL.rsplit("/", 1)[-1].split("?")[0]
_SCHEMA = "core"

# Only this schema's tables, so another module's tables on the shared metadata
# can't be created or dropped out from under it.
_CORE_TABLES = [t for t in Base.metadata.sorted_tables if t.schema == _SCHEMA]


@pytest_asyncio.fixture(scope="session")
async def ensure_test_db():
    try:
        conn = await asyncpg.connect(_ADMIN_DSN)
    except (OSError, asyncpg.PostgresError) as e:
        pytest.skip(f"Postgres unavailable for API key store tests ({_ADMIN_DSN}): {e}")
    try:
        exists = await conn.fetchval("SELECT 1 FROM pg_database WHERE datname = $1", _TEST_DB_NAME)
        if not exists:
            await conn.execute(f'CREATE DATABASE "{_TEST_DB_NAME}"')
    finally:
        await conn.close()


@pytest_asyncio.fixture
async def test_engine(ensure_test_db):
    engine = create_async_engine(TEST_DATABASE_URL, echo=False, poolclass=NullPool)
    async with engine.begin() as conn:
        await conn.execute(text(f"CREATE SCHEMA IF NOT EXISTS {_SCHEMA}"))
        await conn.run_sync(Base.metadata.create_all, tables=_CORE_TABLES)
    yield engine
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all, tables=_CORE_TABLES)
        await conn.execute(text(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE"))
    await engine.dispose()


@pytest_asyncio.fixture
async def session_maker(test_engine, monkeypatch):
    """Point `core.db.session_scope` at the test database for this test."""
    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False, autoflush=False)
    monkeypatch.setattr(db, "get_session_maker", lambda: maker)
    return maker


@pytest_asyncio.fixture
async def session(session_maker) -> AsyncSession:
    """A session for arranging test data; commits are explicit in the test."""
    async with session_maker() as s:
        yield s


@pytest.fixture
def gated_client() -> TestClient:
    """A throwaway app behind the real security guards.

    Lets a database-backed key be exercised through the actual dependency chain
    without starting a module's runtime. `/read` echoes the resolved key's name,
    which is the identity that request logging is allowed to record.
    """
    app = FastAPI()
    register_canopy_error_handler(app)

    @app.get("/read")
    async def read_route(key: APIKey = Security(require_read_access)) -> dict[str, str]:
        return {"name": key.name}

    @app.get("/write")
    async def write_route(key: APIKey = Security(require_write_access)) -> dict[str, str]:
        return {"name": key.name}

    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def keys_client() -> TestClient:
    """The real ``/api-keys`` router, mounted on a throwaway app.

    Exercises the production routes and error handler without pulling in the
    other nine sub-apps.
    """
    app = FastAPI()
    register_canopy_error_handler(app)
    app.include_router(api_keys_router)
    return TestClient(app, raise_server_exceptions=False)


@contextmanager
def db_backed_registry(ttl_s: float = 60.0, env: str = ""):
    """Bind the production registry chain, then put the previous one back.

    The bound registry is process-global, so restoring it keeps these tests from
    leaking a database-backed registry into everything that runs after them.
    ``env`` lets a test authenticate its own requests with a static key (e.g. an
    admin key to call the management routes) alongside the database-backed one
    under test.
    """
    previous = security._registry
    bind_key_registry(ChainedKeyRegistry(EnvKeyRegistry(env), CachingKeyRegistry(DbKeyRegistry(), ttl_s=ttl_s)))
    try:
        yield
    finally:
        security._registry = previous
