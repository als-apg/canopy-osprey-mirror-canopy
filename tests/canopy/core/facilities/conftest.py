"""Fixtures for ``core.facilities`` tests.

A real Postgres *test database* (``canopy_test``, auto-created), with only the
``core`` schema's tables built and dropped per test. Mirrors
``tests/canopy/core/keys/conftest.py`` exactly — same database, same
per-schema table scoping, same skip-if-unreachable behavior.
"""

import os

import pytest
import asyncpg
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.pool import NullPool
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import canopy.core.facilities.models  # noqa: F401 — register core tables on Base.metadata
from canopy.core import db, security
from canopy.core.db import Base, get_session
from canopy.core.errors import register_canopy_error_handler
from canopy.core.modules import WILDCARD, tag_module
from canopy.core.security import APIKey
from canopy.core.facilities import service as facilities_service
from canopy.core.facilities.routes import router as facilities_router

TEST_DATABASE_URL = os.environ.get(
    "CANOPY_TEST_DATABASE_URL", "postgresql+asyncpg://canopy:canopy@localhost:5432/canopy_test"
)
_ADMIN_DSN = os.environ.get("CANOPY_TEST_ADMIN_DSN", "postgresql://canopy:canopy@localhost:5432/postgres")
_TEST_DB_NAME = TEST_DATABASE_URL.rsplit("/", 1)[-1].split("?")[0]
_SCHEMA = "core"

_CORE_TABLES = [t for t in Base.metadata.sorted_tables if t.schema == _SCHEMA]


@pytest.fixture(autouse=True)
def _reset_in_use_checkers():
    """``register_in_use_checker`` appends to a process-global list — a checker
    registered by another test (or by squirrel's app lifespan, elsewhere in the
    suite) must not leak into these tests, which exercise ``core.facilities`` in
    isolation and expect no other module's data to be consulted."""
    previous = list(facilities_service._in_use_checkers)
    facilities_service._in_use_checkers.clear()
    yield
    facilities_service._in_use_checkers[:] = previous


@pytest_asyncio.fixture(scope="session")
async def ensure_test_db():
    try:
        conn = await asyncpg.connect(_ADMIN_DSN)
    except (OSError, asyncpg.PostgresError) as e:
        pytest.skip(f"Postgres unavailable for facility tests ({_ADMIN_DSN}): {e}")
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
    async with session_maker() as s:
        yield s
        await s.rollback()


@pytest.fixture
def fake_keys():
    """Bind a registry serving a read key, a wildcard-facility admin key, an admin
    key with no facility grant, and an admin-only key with no read, then restore.

    The bound registry is process-global, so restoring keeps a facilities-flavored
    registry from leaking into everything that runs after. Four keys: writes are
    read-refused (the read key), wildcard-facility-admin-allowed (the admin key),
    and plain-admin-refused (the scoped-admin key, which holds ``admin`` but not the
    facility wildcard the hierarchy-write routes additionally require). The
    admin-only key has no ``read`` at all, so it exercises ``GET``'s read-or-admin
    gate from the ``admin`` side rather than the ``read`` side.
    """
    keys = {
        "test-read": APIKey(token="test-read", read=True, write=False, modules=frozenset({WILDCARD}), name="read"),
        "test-admin": APIKey(
            token="test-admin",
            read=True,
            write=True,
            admin=True,
            modules=frozenset({WILDCARD}),
            facilities=frozenset({WILDCARD}),
            name="admin",
        ),
        "test-admin-only": APIKey(
            token="test-admin-only",
            read=False,
            write=False,
            admin=True,
            modules=frozenset({WILDCARD}),
            facilities=frozenset({WILDCARD}),
            name="admin-only",
        ),
        "test-scoped-admin": APIKey(
            token="test-scoped-admin",
            read=True,
            write=True,
            admin=True,
            modules=frozenset({WILDCARD}),
            facilities=frozenset(),
            name="scoped-admin",
        ),
    }

    class FakeRegistry:
        async def get(self, token: str) -> APIKey | None:
            return keys.get(token)

    previous = security._registry
    security.bind_key_registry(FakeRegistry())
    try:
        yield
    finally:
        security._registry = previous


@pytest.fixture
def facilities_client(session_maker) -> TestClient:
    """The real ``/v1/facilities`` router, mounted on a throwaway app.

    Wired to the test database via ``get_session``, so a request commits
    against the same schema the ``session`` fixture seeds.
    """
    app = FastAPI()
    register_canopy_error_handler(app)
    tag_module(app, "core")
    app.include_router(facilities_router)

    async def override_get_session():
        async with session_maker() as s:
            try:
                yield s
                await s.commit()
            except Exception:
                await s.rollback()
                raise

    app.dependency_overrides[get_session] = override_get_session
    return TestClient(app, raise_server_exceptions=False)
