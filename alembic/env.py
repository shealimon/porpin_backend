"""Alembic migration environment."""

from __future__ import annotations

import os
import sys
from logging.config import fileConfig
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from alembic import context
from sqlalchemy import engine_from_config, pool

from app.core.pipeline_settings import get_pipeline_settings
from app.db.base import Base

# Import all models so Base.metadata is complete
from app.db import models  # noqa: F401

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def get_url() -> str:
    url = (
        os.environ.get("SUPABASE_DATABASE_URL")
        or os.environ.get("DATABASE_URL")
        or get_pipeline_settings().database_url
    )
    if not url:
        raise RuntimeError("SUPABASE_DATABASE_URL or DATABASE_URL required")
    return url


def run_migrations_offline() -> None:
    url = get_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    configuration = config.get_section(config.config_ini_section) or {}
    configuration["sqlalchemy.url"] = get_url()
    connectable = engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
