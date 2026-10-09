"""Fixtures for the squirrel module's tests.

A real Postgres *test database* (``canopy_test``, auto-created), schema built
per-test with ``create_all``/``drop_all`` for isolation, an httpx ``AsyncClient`` over the
mounted sub-app, and ``dependency_overrides`` swapping the request session for a test one.

Squirrel has no live-EPICS/Redis in the request path (create/restore *enqueue* Arq jobs),
so instead of the source's MockEpicsService + fakeredis we only stub ``jobs.gateway.enqueue``
— the worker tasks that actually touch EPICS are exercised separately with a fake EpicsIO.
"""

import os

import pytest
import asyncpg
import pytest_asyncio
from httpx import AsyncClient, ASGITransport
from sqlalchemy import text
from sqlalchemy.pool import NullPool
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import canopy.squirrel.data.models  # noqa: F401 — register squirrel tables on Base.metadata
from canopy.core.db import Base, get_session
from canopy.squirrel.app import app as squirrel_app

# Overridable so CI (or a non-default Postgres) can point these elsewhere.
TEST_DATABASE_URL = os.environ.get(
    "CANOPY_TEST_DATABASE_URL", "postgresql+asyncpg://canopy:canopy@localhost:5432/canopy_test"
)
_ADMIN_DSN = os.environ.get("CANOPY_TEST_ADMIN_DSN", "postgresql://canopy:canopy@localhost:5432/postgres")
_SCHEMA = "squirrel"

READ_HEADERS = {"X-API-Key": "test-read"}
WRITE_HEADERS = {"X-API-Key": "test-write"}

# Only manage this module's own schema/tables, so a future persistent module's tables
# on the shared Base.metadata can't get created/dropped out from under it.
_SQUIRREL_TABLES = [t for t in Base.metadata.sorted_tables if t.schema == _SCHEMA]


@pytest.fixture(autouse=True)
def disable_restore_hooks(monkeypatch):
    """Keep an ambient ``CANOPY_SQUIRREL__RESTORE_HOOKS`` out of every SnapshotService.

    Tests that want a hook inject it explicitly with ``restore_hooks=[...]``.
    """
    monkeypatch.setattr("canopy.squirrel.services.snapshot_service.get_restore_hooks", lambda: [])


@pytest_asyncio.fixture(scope="session")
async def ensure_test_db():
    """Create the ``canopy_test`` database once per session if it doesn't exist.

    If Postgres is unreachable, skip (rather than fail) every test that needs it, so the
    repo's no-infra ``pytest`` run still passes — these integration tests light up when
    canopy-postgres is running (locally or via the Postgres service in CI). The pure-unit
    tests (restore hooks, the EPICS adapter) don't use this fixture and always run.
    """
    try:
        conn = await asyncpg.connect(_ADMIN_DSN)
    except (OSError, asyncpg.PostgresError) as e:
        pytest.skip(f"Postgres unavailable for squirrel integration tests ({_ADMIN_DSN}): {e}")
    try:
        exists = await conn.fetchval("SELECT 1 FROM pg_database WHERE datname = 'canopy_test'")
        if not exists:
            await conn.execute("CREATE DATABASE canopy_test")
    finally:
        await conn.close()


@pytest_asyncio.fixture
async def test_engine(ensure_test_db):
    """A fresh engine + squirrel schema/tables per test (NullPool — no cross-test connections)."""
    engine = create_async_engine(TEST_DATABASE_URL, echo=False, poolclass=NullPool)
    async with engine.begin() as conn:
        await conn.execute(text(f"CREATE SCHEMA IF NOT EXISTS {_SCHEMA}"))
        await conn.run_sync(Base.metadata.create_all, tables=_SQUIRREL_TABLES)
    yield engine
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all, tables=_SQUIRREL_TABLES)
        await conn.execute(text(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE"))
    await engine.dispose()


@pytest_asyncio.fixture
async def db_session(test_engine) -> AsyncSession:
    """A session for seeding fixture data. Commit to make rows visible to request sessions;
    cross-test isolation comes from ``drop_all``, not rollback (matching the source's model)."""
    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False, autoflush=False)
    async with maker() as session:
        yield session
        await session.rollback()


@pytest.fixture
def enqueued() -> list[tuple]:
    """Records ``(function, args, kwargs)`` for every stubbed ``jobs.gateway.enqueue`` call."""
    return []


@pytest_asyncio.fixture
async def client(test_engine, enqueued, monkeypatch) -> AsyncClient:
    """An AsyncClient over the squirrel sub-app, wired to the test engine + a stubbed enqueue.

    The override mirrors the real ``get_session``: each request gets its own session that
    commits on success and rolls back on error — so one request's failed transaction (e.g. an
    IntegrityError surfaced as 409) can't poison the next. Committed rows persist across requests
    within a test; ``drop_all`` (in ``test_engine``) isolates across tests.
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

    async def fake_enqueue(function, *args, **kwargs):
        enqueued.append((function, args, kwargs))
        return "test-job-id"

    monkeypatch.setattr("canopy.jobs.gateway.enqueue", fake_enqueue)
    squirrel_app.dependency_overrides[get_session] = override_get_session
    try:
        async with AsyncClient(transport=ASGITransport(app=squirrel_app), base_url="http://test") as c:
            yield c
    finally:
        squirrel_app.dependency_overrides.clear()


# --- data helpers (built through the API, like the source's sample_* fixtures) --- #


@pytest_asyncio.fixture
async def sample_tag_group(client: AsyncClient) -> dict:
    r = await client.post("/v1/tags", headers=WRITE_HEADERS, json={"name": "Area", "description": "Areas"})
    assert r.status_code == 200, r.text
    return r.json()


@pytest_asyncio.fixture
async def sample_pv(client: AsyncClient) -> dict:
    r = await client.post(
        "/v1/pvs",
        headers=WRITE_HEADERS,
        json={
            "setpointAddress": "TEST:PV:001:SP",
            "readbackAddress": "TEST:PV:001:RB",
            "device": "TEST-DEVICE-001",
            "description": "Test PV",
            "absTolerance": 0.1,
            "relTolerance": 0.01,
        },
    )
    assert r.status_code == 200, r.text
    return r.json()
