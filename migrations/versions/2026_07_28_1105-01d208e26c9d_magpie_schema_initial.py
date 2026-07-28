"""magpie schema initial

Revision ID: 01d208e26c9d
Revises: 967962fe3434
Create Date: 2026-07-28 11:05:28.518203

"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '01d208e26c9d'
down_revision: str | None = '967962fe3434'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Magpie is the first tenant of its schema; create it before its tables.
    op.execute("CREATE SCHEMA IF NOT EXISTS magpie")
    op.create_table('config',
    sa.Column('slug', sa.String(length=16), nullable=False),
    sa.Column('app', sa.String(length=64), nullable=False),
    sa.Column('name', sa.String(length=255), nullable=True),
    sa.Column('description', sa.Text(), nullable=True),
    sa.Column('data', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('created_by', sa.String(length=255), nullable=True),
    sa.Column('id', sa.UUID(as_uuid=False), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    schema='magpie'
    )
    op.create_index(op.f('ix_magpie_config_app'), 'config', ['app'], unique=False, schema='magpie')
    op.create_index(op.f('ix_magpie_config_name'), 'config', ['name'], unique=False, schema='magpie')
    op.create_index(op.f('ix_magpie_config_slug'), 'config', ['slug'], unique=True, schema='magpie')


def downgrade() -> None:
    op.drop_index(op.f('ix_magpie_config_slug'), table_name='config', schema='magpie')
    op.drop_index(op.f('ix_magpie_config_name'), table_name='config', schema='magpie')
    op.drop_index(op.f('ix_magpie_config_app'), table_name='config', schema='magpie')
    op.drop_table('config', schema='magpie')
    op.execute("DROP SCHEMA IF EXISTS magpie")
