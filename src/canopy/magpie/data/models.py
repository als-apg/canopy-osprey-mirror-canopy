"""Magpie ORM models — the ``magpie`` Postgres schema.

Magpie is a *generic* config store: one table holding opaque JSON blobs for any
consuming app (trace first). The table lives in the ``magpie`` schema
(schema-per-module) and reuses the shared ``canopy.core.db`` declarative base +
mixins so the single Alembic tree autogenerates across modules.
"""

from __future__ import annotations

from sqlalchemy import Text, String
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.dialects.postgresql import JSONB

from canopy.core.db import Base, UUIDMixin, TimestampMixin

MAGPIE_SCHEMA = "magpie"


class Config(UUIDMixin, TimestampMixin, Base):
    """A stored config for a consuming app.

    ``id`` (UUID) is the internal primary key; ``slug`` is the short, unguessable
    public token used in shareable URLs (e.g. ``trace?config=<slug>``) and is
    globally unique. ``app`` tags the owning consumer so a caller can only resolve
    its own app's configs. ``data`` is an opaque JSONB blob — magpie neither
    defines nor validates its internal shape; the consuming app owns that.
    """

    __tablename__ = "config"
    __table_args__ = {"schema": MAGPIE_SCHEMA}

    slug: Mapped[str] = mapped_column(String(16), nullable=False, unique=True, index=True)
    app: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    name: Mapped[str | None] = mapped_column(String(255), index=True)
    description: Mapped[str | None] = mapped_column(Text)
    data: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_by: Mapped[str | None] = mapped_column(String(255))
