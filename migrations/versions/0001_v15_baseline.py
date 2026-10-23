"""V1.5 baseline; adopt pre-existing SQLite/PostgreSQL V1.0..V1.4 data.

Revision ID: 0001_v15_baseline
Revises:
"""
from alembic import op
from app import Base, upgrade_existing_schema

revision = '0001_v15_baseline'
down_revision = None
branch_labels = None
depends_on = None

def upgrade():
    conn = op.get_bind()
    Base.metadata.create_all(bind=conn, checkfirst=True)
    upgrade_existing_schema(conn)

def downgrade():
    raise RuntimeError('Baseline downgrade intentionally disabled: preserve business data and restore a backup instead')
