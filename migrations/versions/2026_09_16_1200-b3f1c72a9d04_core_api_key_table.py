"""core schema: api_key table

Revision ID: b3f1c72a9d04
Revises: 01d208e26c9d
Create Date: 2026-09-16 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b3f1c72a9d04"
down_revision: str | None = "01d208e26c9d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # API keys are core infrastructure rather than a module's data; the `core`
    # schema is created here as its first tenant.
    op.execute("CREATE SCHEMA IF NOT EXISTS core")
    op.create_table(
        "api_key",
        sa.Column("app_name", sa.String(length=255), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("read_access", sa.Boolean(), nullable=False),
        sa.Column("write_access", sa.Boolean(), nullable=False),
        sa.Column("admin_access", sa.Boolean(), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.UUID(as_uuid=False), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        schema="core",
    )
    # app_name is indexed but NOT unique, deliberately: a rotation needs the
    # outgoing and incoming key for one client active at the same time.
    op.create_index(op.f("ix_core_api_key_app_name"), "api_key", ["app_name"], unique=False, schema="core")
    op.create_index(op.f("ix_core_api_key_token_hash"), "api_key", ["token_hash"], unique=True, schema="core")


def downgrade() -> None:
    op.drop_index(op.f("ix_core_api_key_token_hash"), table_name="api_key", schema="core")
    op.drop_index(op.f("ix_core_api_key_app_name"), table_name="api_key", schema="core")
    op.drop_table("api_key", schema="core")
    op.execute("DROP SCHEMA IF EXISTS core")
