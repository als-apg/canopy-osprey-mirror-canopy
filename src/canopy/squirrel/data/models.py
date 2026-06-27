"""Squirrel ORM models — the ``squirrel`` Postgres schema.

Every table lives in the ``squirrel`` schema (schema-per-module)
and reuses the shared ``canopy.core.db`` declarative base + mixins so the single
Alembic tree autogenerates across modules. Cross-*module* references are opaque ids,
never cross-schema foreign keys — but the foreign keys *within* squirrel
(snapshot→value, pv↔tag, …) port as-is.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Text,
    Float,
    Table,
    Column,
    String,
    Boolean,
    Integer,
    DateTime,
    ForeignKey,
    text,
)
from sqlalchemy.orm import Mapped, relationship, mapped_column
from sqlalchemy.dialects.postgresql import UUID, JSONB

from canopy.core.db import Base, UUIDMixin, TimestampMixin

SQUIRREL_SCHEMA = "squirrel"


def _fk(target: str) -> str:
    """Schema-qualify an intra-squirrel foreign-key target (e.g. ``pv.id``)."""
    return f"{SQUIRREL_SCHEMA}.{target}"


# --------------------------------------------------------------------------- #
# PV registry + tagging
# --------------------------------------------------------------------------- #

# Many-to-many between pv and tag. Composite PK (pv_id, tag_id); both sides
# cascade so deleting either end removes the link rows at the database.
pv_tag = Table(
    "pv_tag",
    Base.metadata,
    Column("pv_id", UUID(as_uuid=False), ForeignKey(_fk("pv.id"), ondelete="CASCADE"), primary_key=True),
    Column("tag_id", UUID(as_uuid=False), ForeignKey(_fk("tag.id"), ondelete="CASCADE"), primary_key=True),
    schema=SQUIRREL_SCHEMA,
)


class Pv(UUIDMixin, TimestampMixin, Base):
    """A registered PV: its address(es), owning device, and compare tolerances.

    Address resolution (the three ``*_address`` columns + ``device``) is kept
    independent of the comparison policy (``abs_tolerance``/``rel_tolerance``) so a
    later Raccoon directory delegation can resolve addresses externally while the
    tolerances stay local. Only ``setpoint_address`` is unique; the
    at-least-one-address rule is enforced in the service layer, not the DB.
    """

    __tablename__ = "pv"
    __table_args__ = {"schema": SQUIRREL_SCHEMA}

    setpoint_address: Mapped[str | None] = mapped_column(String(255), index=True, unique=True)
    readback_address: Mapped[str | None] = mapped_column(String(255), index=True)
    config_address: Mapped[str | None] = mapped_column(String(255), index=True)
    device: Mapped[str | None] = mapped_column(String(255), index=True)
    description: Mapped[str | None] = mapped_column(Text)
    abs_tolerance: Mapped[float] = mapped_column(Float, nullable=False, server_default=text("0.0"))
    rel_tolerance: Mapped[float] = mapped_column(Float, nullable=False, server_default=text("0.0"))

    tags: Mapped[list[Tag]] = relationship("Tag", secondary=pv_tag, back_populates="pvs", lazy="selectin")
    snapshot_values: Mapped[list[SnapshotValue]] = relationship(
        "SnapshotValue", back_populates="pv", cascade="all, delete-orphan"
    )


class TagGroup(UUIDMixin, TimestampMixin, Base):
    """A named group of tags (e.g. ``Region``); group name is globally unique."""

    __tablename__ = "tag_group"
    __table_args__ = {"schema": SQUIRREL_SCHEMA}

    name: Mapped[str] = mapped_column(String(255), nullable=False, index=True, unique=True)
    description: Mapped[str | None] = mapped_column(Text)

    tags: Mapped[list[Tag]] = relationship(
        "Tag", back_populates="group", cascade="all, delete-orphan", lazy="selectin"
    )


class Tag(UUIDMixin, TimestampMixin, Base):
    """A tag within a group. Per-group name uniqueness is enforced in the repo."""

    __tablename__ = "tag"
    __table_args__ = {"schema": SQUIRREL_SCHEMA}

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    group_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey(_fk("tag_group.id"), ondelete="CASCADE"), nullable=False, index=True
    )

    group: Mapped[TagGroup] = relationship("TagGroup", back_populates="tags", lazy="selectin")
    pvs: Mapped[list[Pv]] = relationship("Pv", secondary=pv_tag, back_populates="tags")


# --------------------------------------------------------------------------- #
# Snapshots
# --------------------------------------------------------------------------- #


class Snapshot(UUIDMixin, TimestampMixin, Base):
    """A captured machine state: metadata + one ``SnapshotValue`` per read address."""

    __tablename__ = "snapshot"
    __table_args__ = {"schema": SQUIRREL_SCHEMA}

    title: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    description: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[str | None] = mapped_column(String(255))

    values: Mapped[list[SnapshotValue]] = relationship(
        "SnapshotValue", back_populates="snapshot", cascade="all, delete-orphan", lazy="selectin"
    )


class SnapshotValue(UUIDMixin, Base):
    """One captured reading.

    ``setpoint_value``/``readback_value`` are free-form JSONB blobs (shaped
    ``{"value", "status", "severity", "timestamp", "units", "precision"}``) so
    metadata fidelity can grow without a migration. ``pv_name`` denormalizes the
    captured address for display/query without joining ``pv``. There are no row
    timestamps — the ``timestamp`` column holds the capture time, not a row audit.
    """

    __tablename__ = "snapshot_value"
    __table_args__ = {"schema": SQUIRREL_SCHEMA}

    snapshot_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey(_fk("snapshot.id"), ondelete="CASCADE"), nullable=False, index=True
    )
    pv_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey(_fk("pv.id"), ondelete="CASCADE"), nullable=False, index=True
    )
    pv_name: Mapped[str] = mapped_column(String(255), nullable=False)
    setpoint_value: Mapped[dict | None] = mapped_column(JSONB)
    readback_value: Mapped[dict | None] = mapped_column(JSONB)
    status: Mapped[int | None] = mapped_column(Integer)
    severity: Mapped[int | None] = mapped_column(Integer)
    timestamp: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    snapshot: Mapped[Snapshot] = relationship("Snapshot", back_populates="values")
    pv: Mapped[Pv] = relationship("Pv", back_populates="snapshot_values")


# --------------------------------------------------------------------------- #
# Meta-PVs
# --------------------------------------------------------------------------- #


class MetaPv(Base):
    """Marks a PV as a 'meta' PV shown specially on snapshots.

    Keyed directly by ``pv_id`` (no surrogate id, no timestamps). The FK
    intentionally has no cascade — a meta marker must be removed before its PV can
    be deleted.
    """

    __tablename__ = "meta_pv"
    __table_args__ = {"schema": SQUIRREL_SCHEMA}

    pv_id: Mapped[str] = mapped_column(UUID(as_uuid=False), ForeignKey(_fk("pv.id")), primary_key=True)
    display_name: Mapped[str] = mapped_column(String(32), nullable=False, server_default=text("''"))
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
