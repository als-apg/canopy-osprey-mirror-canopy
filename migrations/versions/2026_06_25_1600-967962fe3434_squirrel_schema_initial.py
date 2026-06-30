"""squirrel schema initial

Revision ID: 967962fe3434
Revises:
Create Date: 2026-06-25 16:00:18.412980

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "967962fe3434"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Squirrel is the first tenant of its schema; create it before its tables.
    op.execute("CREATE SCHEMA IF NOT EXISTS squirrel")
    op.create_table(
        "pv",
        sa.Column("setpoint_address", sa.String(length=255), nullable=True),
        sa.Column("readback_address", sa.String(length=255), nullable=True),
        sa.Column("config_address", sa.String(length=255), nullable=True),
        sa.Column("device", sa.String(length=255), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("abs_tolerance", sa.Float(), server_default=sa.text("0.0"), nullable=False),
        sa.Column("rel_tolerance", sa.Float(), server_default=sa.text("0.0"), nullable=False),
        sa.Column("id", sa.UUID(as_uuid=False), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        schema="squirrel",
    )
    op.create_index(op.f("ix_squirrel_pv_config_address"), "pv", ["config_address"], unique=False, schema="squirrel")
    op.create_index(op.f("ix_squirrel_pv_device"), "pv", ["device"], unique=False, schema="squirrel")
    op.create_index(
        op.f("ix_squirrel_pv_readback_address"), "pv", ["readback_address"], unique=False, schema="squirrel"
    )
    op.create_index(op.f("ix_squirrel_pv_setpoint_address"), "pv", ["setpoint_address"], unique=True, schema="squirrel")
    op.create_table(
        "snapshot",
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("created_by", sa.String(length=255), nullable=True),
        sa.Column("id", sa.UUID(as_uuid=False), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        schema="squirrel",
    )
    op.create_index(op.f("ix_squirrel_snapshot_title"), "snapshot", ["title"], unique=False, schema="squirrel")
    op.create_table(
        "tag_group",
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("id", sa.UUID(as_uuid=False), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        schema="squirrel",
    )
    op.create_index(op.f("ix_squirrel_tag_group_name"), "tag_group", ["name"], unique=True, schema="squirrel")
    op.create_table(
        "meta_pv",
        sa.Column("pv_id", sa.UUID(as_uuid=False), nullable=False),
        sa.Column("display_name", sa.String(length=32), server_default=sa.text("''"), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.ForeignKeyConstraint(
            ["pv_id"],
            ["squirrel.pv.id"],
        ),
        sa.PrimaryKeyConstraint("pv_id"),
        schema="squirrel",
    )
    op.create_table(
        "snapshot_value",
        sa.Column("snapshot_id", sa.UUID(as_uuid=False), nullable=False),
        sa.Column("pv_id", sa.UUID(as_uuid=False), nullable=False),
        sa.Column("pv_name", sa.String(length=255), nullable=False),
        sa.Column("setpoint_value", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("readback_value", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("status", sa.Integer(), nullable=True),
        sa.Column("severity", sa.Integer(), nullable=True),
        sa.Column("timestamp", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.UUID(as_uuid=False), nullable=False),
        sa.ForeignKeyConstraint(["pv_id"], ["squirrel.pv.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["snapshot_id"], ["squirrel.snapshot.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        schema="squirrel",
    )
    op.create_index(
        op.f("ix_squirrel_snapshot_value_pv_id"), "snapshot_value", ["pv_id"], unique=False, schema="squirrel"
    )
    op.create_index(
        op.f("ix_squirrel_snapshot_value_snapshot_id"),
        "snapshot_value",
        ["snapshot_id"],
        unique=False,
        schema="squirrel",
    )
    op.create_table(
        "tag",
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("group_id", sa.UUID(as_uuid=False), nullable=False),
        sa.Column("id", sa.UUID(as_uuid=False), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["group_id"], ["squirrel.tag_group.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        schema="squirrel",
    )
    op.create_index(op.f("ix_squirrel_tag_group_id"), "tag", ["group_id"], unique=False, schema="squirrel")
    op.create_table(
        "pv_tag",
        sa.Column("pv_id", sa.UUID(as_uuid=False), nullable=False),
        sa.Column("tag_id", sa.UUID(as_uuid=False), nullable=False),
        sa.ForeignKeyConstraint(["pv_id"], ["squirrel.pv.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["tag_id"], ["squirrel.tag.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("pv_id", "tag_id"),
        schema="squirrel",
    )
    # NOTE: Alembic's autogenerate flags its own public.alembic_version table as
    # "removed" under include_schemas=True. It is migration state — never drop it.


def downgrade() -> None:
    op.drop_table("pv_tag", schema="squirrel")
    op.drop_index(op.f("ix_squirrel_tag_group_id"), table_name="tag", schema="squirrel")
    op.drop_table("tag", schema="squirrel")
    op.drop_index(op.f("ix_squirrel_snapshot_value_snapshot_id"), table_name="snapshot_value", schema="squirrel")
    op.drop_index(op.f("ix_squirrel_snapshot_value_pv_id"), table_name="snapshot_value", schema="squirrel")
    op.drop_table("snapshot_value", schema="squirrel")
    op.drop_table("meta_pv", schema="squirrel")
    op.drop_index(op.f("ix_squirrel_tag_group_name"), table_name="tag_group", schema="squirrel")
    op.drop_table("tag_group", schema="squirrel")
    op.drop_index(op.f("ix_squirrel_snapshot_title"), table_name="snapshot", schema="squirrel")
    op.drop_table("snapshot", schema="squirrel")
    op.drop_index(op.f("ix_squirrel_pv_setpoint_address"), table_name="pv", schema="squirrel")
    op.drop_index(op.f("ix_squirrel_pv_readback_address"), table_name="pv", schema="squirrel")
    op.drop_index(op.f("ix_squirrel_pv_device"), table_name="pv", schema="squirrel")
    op.drop_index(op.f("ix_squirrel_pv_config_address"), table_name="pv", schema="squirrel")
    op.drop_table("pv", schema="squirrel")
    op.execute("DROP SCHEMA IF EXISTS squirrel")
