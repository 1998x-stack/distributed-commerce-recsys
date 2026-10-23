"""Durable rebuild gate for coordinating full and incremental OpenSearch updates.

Revision ID: 0002_v17_search_gate
Revises: 0001_v15_baseline
"""
from alembic import op
import sqlalchemy as sa

revision = '0002_v17_search_gate'
down_revision = '0001_v15_baseline'
branch_labels = None
depends_on = None


def upgrade():
    conn = op.get_bind()
    if not sa.inspect(conn).has_table('search_maintenance'):
        op.create_table('search_maintenance',
                        sa.Column('id', sa.Integer(), primary_key=True),
                        sa.Column('owner', sa.String(36), nullable=True),
                        sa.Column('lease_until', sa.DateTime(timezone=True), nullable=True))
    conn.execute(sa.text('INSERT INTO search_maintenance (id) VALUES (1) ON CONFLICT (id) DO NOTHING'))


def downgrade():
    raise RuntimeError('Search gate downgrade disabled: restore a database backup if needed')
