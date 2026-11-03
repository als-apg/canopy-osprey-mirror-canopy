"""Facility ORM model — the ``core`` Postgres schema."""

from __future__ import annotations

from sqlalchemy import Text, String, ForeignKey
from sqlalchemy.orm import Mapped, relationship, mapped_column
from sqlalchemy.dialects.postgresql import UUID

from canopy.core.db import Base, UUIDMixin, TimestampMixin

CORE_SCHEMA = "core"


def _fk(target: str) -> str:
    """Schema-qualify an intra-core foreign-key target (e.g. ``facility.id``)."""
    return f"{CORE_SCHEMA}.{target}"


class Facility(UUIDMixin, TimestampMixin, Base):
    """A facility or sub-facility node (e.g. LCLS, then LCLS-SC/LCLS-NC beneath it).

    Self-referential rather than two separate tables — "in facility X" means "X or
    any descendant of X," which falls out of walking ``parent_id`` without a
    facility/sub-facility special case. ``slug`` is immutable once created: it goes
    into URLs and API key facility grants.
    """

    __tablename__ = "facility"
    __table_args__ = {"schema": CORE_SCHEMA}

    slug: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    parent_id: Mapped[str | None] = mapped_column(
        UUID(as_uuid=False), ForeignKey(_fk("facility.id"), ondelete="RESTRICT"), index=True
    )

    parent: Mapped[Facility | None] = relationship("Facility", remote_side="Facility.id", back_populates="children")
    children: Mapped[list[Facility]] = relationship("Facility", back_populates="parent")
