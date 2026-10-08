"""api_key: add modules column

Revision ID: e2a7c1f5b6d3
Revises: b3f1c72a9d04
Create Date: 2026-09-17 20:27:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e2a7c1f5b6d3"
down_revision: str | None = "b3f1c72a9d04"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # server_default backfills every existing row to {"*"} — every module — so
    # this is additive with no behaviour change for a key minted before this
    # column existed. No index: the lookup is always by token_hash.
    op.add_column(
        "api_key",
        sa.Column(
            "modules",
            sa.ARRAY(sa.String()),
            server_default=sa.text("'{*}'::text[]"),
            nullable=False,
        ),
        schema="core",
    )


def downgrade() -> None:
    op.drop_column("api_key", "modules", schema="core")
