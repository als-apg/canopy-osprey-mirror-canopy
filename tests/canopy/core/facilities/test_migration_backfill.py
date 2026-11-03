"""Regression test for the ``aecc42bb60da`` migration's facility-grant backfill.

A key minted before this migration existed must keep seeing every facility after
upgrade — the same "no silent behavior change" guarantee the sibling ``modules``
migration (``e2a7c1f5b6d3``) gives. Driven through the real ``alembic`` CLI against
a throwaway database, not the ORM, since the thing under test is the SQL the
migration emits, not what the current model declares.
"""

import os
import subprocess

import pytest
import asyncpg

_ADMIN_DSN = os.environ.get("CANOPY_TEST_ADMIN_DSN", "postgresql://canopy:canopy@localhost:5432/postgres")
_TEST_DB_NAME = "canopy_migration_test"
_TEST_DATABASE_URL = (
    os.environ.get("CANOPY_TEST_DATABASE_URL", "postgresql+asyncpg://canopy:canopy@localhost:5432/").rsplit("/", 1)[0]
    + f"/{_TEST_DB_NAME}"
)

# One revision before the facility table lands, and the facility table revision itself.
_PRE_FACILITY_REVISION = "e2a7c1f5b6d3"
_FACILITY_REVISION = "aecc42bb60da"


def _run_alembic(*args: str) -> None:
    subprocess.run(
        ["uv", "run", "alembic", *args],
        check=True,
        env={**os.environ, "CANOPY_DATABASE_URL": _TEST_DATABASE_URL},
        capture_output=True,
        text=True,
    )


@pytest.fixture
async def migration_db():
    try:
        conn = await asyncpg.connect(_ADMIN_DSN)
    except (OSError, asyncpg.PostgresError) as e:
        pytest.skip(f"Postgres unavailable for migration tests ({_ADMIN_DSN}): {e}")
    try:
        await conn.execute(f'DROP DATABASE IF EXISTS "{_TEST_DB_NAME}"')
        await conn.execute(f'CREATE DATABASE "{_TEST_DB_NAME}"')
    finally:
        await conn.close()
    yield
    conn = await asyncpg.connect(_ADMIN_DSN)
    try:
        await conn.execute(f'DROP DATABASE IF EXISTS "{_TEST_DB_NAME}"')
    finally:
        await conn.close()


async def test_upgrade_backfills_existing_keys_to_every_facility(migration_db):
    dsn = _TEST_DATABASE_URL.replace("postgresql+asyncpg://", "postgresql://")

    _run_alembic("upgrade", _PRE_FACILITY_REVISION)

    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute(
            """
            INSERT INTO core.api_key (id, app_name, token_hash, is_active, read_access, write_access, admin_access, modules)
            VALUES (gen_random_uuid(), 'legacy', 'deadbeef', true, true, false, false, '{*}')
            """
        )
    finally:
        await conn.close()

    _run_alembic("upgrade", _FACILITY_REVISION)

    conn = await asyncpg.connect(dsn)
    try:
        legacy_facilities = await conn.fetchval("SELECT facilities FROM core.api_key WHERE app_name = 'legacy'")
        assert legacy_facilities == ["*"]

        await conn.execute(
            """
            INSERT INTO core.api_key (id, app_name, token_hash, is_active, read_access, write_access, admin_access, modules)
            VALUES (gen_random_uuid(), 'new-key', 'cafef00d', true, true, false, false, '{*}')
            """
        )
        new_key_facilities = await conn.fetchval("SELECT facilities FROM core.api_key WHERE app_name = 'new-key'")
        assert new_key_facilities == []
    finally:
        await conn.close()
