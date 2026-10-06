"""API key ORM model — the ``core`` Postgres schema.

Reuses the shared ``canopy.core.db`` declarative base + mixins so the single
Alembic tree autogenerates this alongside every module's tables.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import ARRAY, String, Boolean, DateTime, text
from sqlalchemy.orm import Mapped, mapped_column

from canopy.core.db import Base, UUIDMixin, TimestampMixin
from canopy.core.modules import WILDCARD

CORE_SCHEMA = "core"


class ApiKey(UUIDMixin, TimestampMixin, Base):
    """A minted API key. Only the token's SHA-256 hash is stored.

    ``app_name`` is a human-readable label for the client class holding the key.
    It is indexed but deliberately **not** unique among active rows: zero-downtime
    rotation needs the outgoing and incoming key for the same client valid at the
    same time, and a uniqueness constraint would force you to break the client
    (deactivate first) or mint under a throwaway name.

    ``last_used_at`` is what makes "is it safe to revoke the old key yet?"
    answerable. It is written on a registry cache miss, so it lags real traffic by
    up to the cache TTL — minutes of granularity, which is all that question needs.
    """

    __tablename__ = "api_key"
    __table_args__ = {"schema": CORE_SCHEMA}

    app_name: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    read_access: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    write_access: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    admin_access: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Which mounted sub-apps (plus "core" for the parent app's own routes) this
    # key may call. {"*"} — the default, matching every key minted before this
    # column existed — means every module. Not indexed: the lookup is always by
    # `token_hash`, and the filter runs in Python against the cached record.
    modules: Mapped[list[str]] = mapped_column(
        ARRAY(String),
        nullable=False,
        default=lambda: [WILDCARD],
        server_default=text(f"'{{{WILDCARD}}}'::text[]"),
    )
