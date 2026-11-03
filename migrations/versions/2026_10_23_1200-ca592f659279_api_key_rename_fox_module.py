"""api_key: rename the fox module scope to formula-engine

Revision ID: ca592f659279
Revises: aecc42bb60da
Create Date: 2026-10-23 12:00:00.000000

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "ca592f659279"
down_revision: str | None = "aecc42bb60da"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # The Fox sub-app is now mounted as /formula-engine with module tag
    # "formula-engine". A stored key scoped to "fox" would otherwise silently
    # lose access to it, so carry the scope over in place.
    op.execute(
        "UPDATE core.api_key SET modules = array_replace(modules, 'fox', 'formula-engine') WHERE 'fox' = ANY(modules)"
    )


def downgrade() -> None:
    op.execute(
        "UPDATE core.api_key SET modules = array_replace(modules, 'formula-engine', 'fox') "
        "WHERE 'formula-engine' = ANY(modules)"
    )
