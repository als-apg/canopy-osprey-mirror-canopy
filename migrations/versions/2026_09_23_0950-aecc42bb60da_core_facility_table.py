"""core facility table

Scoped to the ``core`` schema only: the ``core.facility`` hierarchy and the
``core.api_key.facilities`` grant column. Squirrel's references to a facility
(``squirrel.pv_facility``, ``squirrel.snapshot.facility_id``) land in the
follow-on revision, so the facility table and the module that consumes it can be
reviewed and reverted independently.

Revision ID: aecc42bb60da
Revises: e2a7c1f5b6d3
Create Date: 2026-09-23 09:50:19.059017

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "aecc42bb60da"
down_revision: str | None = "e2a7c1f5b6d3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "facility",
        sa.Column("slug", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("parent_id", sa.UUID(as_uuid=False), nullable=True),
        sa.Column("id", sa.UUID(as_uuid=False), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["parent_id"], ["core.facility.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        schema="core",
    )
    op.create_index(op.f("ix_core_facility_parent_id"), "facility", ["parent_id"], unique=False, schema="core")
    op.create_index(op.f("ix_core_facility_slug"), "facility", ["slug"], unique=True, schema="core")
    # Backfill every pre-existing row to `{"*"}` — every facility — the same way
    # `e2a7c1f5b6d3` backfilled `modules`, so a key minted before this column existed
    # keeps seeing everything it could before. Only new rows should default to `{}`
    # ("provisioned but not yet assigned"), so the server default is narrowed to that
    # right after the backfill, in the same migration.
    op.add_column(
        "api_key",
        sa.Column("facilities", sa.ARRAY(sa.String()), server_default=sa.text("'{*}'::text[]"), nullable=False),
        schema="core",
    )
    op.alter_column("api_key", "facilities", server_default=sa.text("'{}'::text[]"), schema="core")


def downgrade() -> None:
    op.drop_column("api_key", "facilities", schema="core")
    op.drop_index(op.f("ix_core_facility_slug"), table_name="facility", schema="core")
    op.drop_index(op.f("ix_core_facility_parent_id"), table_name="facility", schema="core")
    op.drop_table("facility", schema="core")
