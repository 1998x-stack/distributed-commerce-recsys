"""Catalog soft-deletion flag, preserving historical product references.

Revision ID: 0003_v18_catalog_visibility
Revises: 0002_v17_search_gate
"""
from alembic import op
import sqlalchemy as sa

revision = '0003_v18_catalog_visibility'
down_revision = '0002_v17_search_gate'
branch_labels = None
depends_on = None


def upgrade():
    conn = op.get_bind()
    columns = {col['name'] for col in sa.inspect(conn).get_columns('products')}
    if 'is_active' not in columns:
        op.add_column('products', sa.Column('is_active', sa.Boolean(), nullable=False, server_default=sa.text('1')))
    op.create_index('ix_products_is_active', 'products', ['is_active'], unique=False, if_not_exists=True)


def downgrade():
    raise RuntimeError('Catalog visibility downgrade intentionally disabled; restore a backup')
