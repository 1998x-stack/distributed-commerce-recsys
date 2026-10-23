"""Alembic database URL follows the exact same config as the commerce API."""
from __future__ import annotations
from logging.config import fileConfig
from alembic import context
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services" / "api") if (Path(__file__).resolve().parents[1] / "services" / "api").is_dir() else str(Path(__file__).resolve().parents[1]))
from sqlalchemy import pool, create_engine
from app import Base, DSN, engine

config = context.config
if config.config_file_name:
    fileConfig(config.config_file_name)
target_metadata = Base.metadata

def run_migrations_offline():
    context.configure(url=DSN, target_metadata=target_metadata, literal_binds=True, dialect_opts={'paramstyle': 'named'})
    with context.begin_transaction():
        context.run_migrations()

def run_migrations_online():
    # Use the same engine as API settings (including SQLite PRAGMAs).
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
        with context.begin_transaction():
            context.run_migrations()

if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
