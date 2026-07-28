"""Fixtures for the magpie module's tests.

A real Postgres *test database* (``canopy_test``, auto-created), schema built
per-test with ``create_all``/``drop_all`` for isolation, an httpx ``AsyncClient`` over the
mounted sub-app, and ``dependency_overrides`` swapping the request session for a test one.

Magpie is pure synchronous CRUD — no worker/EPICS/Redis in the request path — so unlike
squirrel there is no ``jobs.gateway.enqueue`` to stub.
"""

import os

import pytest
import asyncpg
import pytest_asyncio
from httpx import AsyncClient, ASGITransport
from sqlalchemy import text
from sqlalchemy.pool import NullPool
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import canopy.magpie.data.models  # noqa: F401 — register magpie tables on Base.metadata
from canopy.core.db import Base, get_session
from canopy.magpie.app import app as magpie_app

# Overridable so CI (or a non-default Postgres) can point these elsewhere.
TEST_DATABASE_URL = os.environ.get(
    "CANOPY_TEST_DATABASE_URL", "postgresql+asyncpg://canopy:canopy@localhost:5432/canopy_test"
)
_ADMIN_DSN = os.environ.get("CANOPY_TEST_ADMIN_DSN", "postgresql://canopy:canopy@localhost:5432/postgres")
_SCHEMA = "magpie"

READ_HEADERS = {"X-API-Key": "test-read"}
WRITE_HEADERS = {"X-API-Key": "test-write"}

# Only manage this module's own schema/tables, so another persistent module's tables
# on the shared Base.metadata can't get created/dropped out from under it.
_MAGPIE_TABLES = [t for t in Base.metadata.sorted_tables if t.schema == _SCHEMA]


@pytest_asyncio.fixture(scope="session")
async def ensure_test_db():
    """Create the ``canopy_test`` database once per session if it doesn't exist.

    If Postgres is unreachable, skip (rather than fail) every test that needs it, so the
    repo's no-infra ``pytest`` run still passes — these integration tests light up when
    canopy-postgres is running (locally or via the Postgres service in CI).
    """
    try:
        conn = await asyncpg.connect(_ADMIN_DSN)
    except (OSError, asyncpg.PostgresError) as e:
        pytest.skip(f"Postgres unavailable for magpie integration tests ({_ADMIN_DSN}): {e}")
    try:
        exists = await conn.fetchval("SELECT 1 FROM pg_database WHERE datname = 'canopy_test'")
        if not exists:
            await conn.execute("CREATE DATABASE canopy_test")
    finally:
        await conn.close()


@pytest_asyncio.fixture
async def test_engine(ensure_test_db):
    """A fresh engine + magpie schema/tables per test (NullPool — no cross-test connections)."""
    engine = create_async_engine(TEST_DATABASE_URL, echo=False, poolclass=NullPool)
    async with engine.begin() as conn:
        await conn.execute(text(f"CREATE SCHEMA IF NOT EXISTS {_SCHEMA}"))
        await conn.run_sync(Base.metadata.create_all, tables=_MAGPIE_TABLES)
    yield engine
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all, tables=_MAGPIE_TABLES)
        await conn.execute(text(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE"))
    await engine.dispose()


@pytest_asyncio.fixture
async def client(test_engine) -> AsyncClient:
    """An AsyncClient over the magpie sub-app, wired to the test engine.

    The override mirrors the real ``get_session``: each request gets its own session that
    commits on success and rolls back on error — so one request's failed transaction can't
    poison the next. Committed rows persist across requests within a test; ``drop_all``
    (in ``test_engine``) isolates across tests.
    """
    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False, autoflush=False)

    async def override_get_session():
        async with maker() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    magpie_app.dependency_overrides[get_session] = override_get_session
    try:
        async with AsyncClient(transport=ASGITransport(app=magpie_app), base_url="http://test") as c:
            yield c
    finally:
        magpie_app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def sample_config(client: AsyncClient) -> dict:
    r = await client.post(
        "/v1/configs/trace",
        headers=WRITE_HEADERS,
        json={
            "name": "my workspace",
            "description": "seed config",
            "data": {"pvs": ["TEST:A", "TEST:B"], "timeRange": {"start": "-1h", "end": "now"}},
            "createdBy": "tester",
        },
    )
    assert r.status_code == 200, r.text
    return r.json()
