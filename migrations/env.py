"""Alembic environment for Canopy's single, multi-module migration tree.

One Postgres database, one schema per persistent module. All modules' ORM
metadata lives on the shared ``canopy.core.db.Base``, so a single autogenerate
pass sees every module's tables; ``include_schemas=True`` makes the comparison
schema-aware, and the lone ``alembic_version`` table is pinned to ``public``.
"""

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

# Register every persistent module's models on Base.metadata so --autogenerate
# sees them. Add an import here when a module gains a data tier.
import canopy.core.keys.models  # noqa: F401, E402
import canopy.magpie.data.models  # noqa: F401, E402
import canopy.squirrel.data.models  # noqa: F401, E402
from canopy.core.db import Base
from canopy.core.config import get_settings

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata

# Pull the DB URL from Canopy settings, but deliberately do NOT round-trip it
# through config.set_main_option(): that stores into ConfigParser, whose
# %-interpolation would mangle (or reject) a password containing a literal '%'.
# We inject it straight into the engine config / migration context below instead.
database_url = get_settings().database_url


def _include_object(obj, name, type_, reflected, compare_to) -> bool:
    """Exclude Alembic's own version table from autogenerate comparison.

    ``alembic_version`` lives in ``public`` and belongs to no module's metadata;
    under ``include_schemas=True`` autogenerate would otherwise propose dropping it
    on every run (and ``alembic check`` would always fail). Never manage it.
    """
    if type_ == "table" and name == "alembic_version":
        return False
    return True


def run_migrations_offline() -> None:
    """Emit SQL without a live connection (alembic upgrade --sql)."""
    context.configure(
        url=database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        include_schemas=True,
        version_table_schema="public",
        include_object=_include_object,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        include_schemas=True,
        version_table_schema="public",
        include_object=_include_object,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    configuration = config.get_section(config.config_ini_section, {})
    configuration["sqlalchemy.url"] = database_url
    connectable = async_engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
