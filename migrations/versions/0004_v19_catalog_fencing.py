"""Durable per-product version fencing for Gorse and OpenSearch.

Revision ID: 0004_v19_catalog_fencing
Revises: 0003_v18_catalog_visibility
"""
from alembic import op
import sqlalchemy as sa

revision = '0004_v19_catalog_fencing'
down_revision = '0003_v18_catalog_visibility'
branch_labels = None
depends_on = None

def upgrade():
    conn = op.get_bind()
    products = {c['name'] for c in sa.inspect(conn).get_columns('products')}
    outbox = {c['name'] for c in sa.inspect(conn).get_columns('outbox')}
    if 'catalog_revision' not in products:
        op.add_column('products', sa.Column('catalog_revision', sa.Integer(),
                      nullable=False, server_default='1'))
    if 'catalog_revision' not in outbox:
        op.add_column('outbox', sa.Column('catalog_revision', sa.Integer(),
                      nullable=False, server_default='1'))

def downgrade():
    raise RuntimeError('Catalog fencing downgrade intentionally disabled; restore a backup')
