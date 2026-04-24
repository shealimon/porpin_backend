"""PostgreSQL (Supabase) engine for API + worker — standard psycopg2 URI from SUPABASE_DATABASE_URL."""

from __future__ import annotations

import logging
from collections.abc import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.pipeline_settings import get_pipeline_settings
from app.db.base import Base  # noqa: F401 — used in create_all_tables

logger = logging.getLogger(__name__)

_engine = None
_session_factory = None
_schema_ensured_on_connect = False


def get_engine():
    global _engine
    if _engine is None:
        url = get_pipeline_settings().database_url
        if not url:
            return None
        settings = get_pipeline_settings()
        # Avoid hanging forever on unreachable Supabase/Postgres (blocks ASGI /health).
        _engine = create_engine(
            url,
            pool_pre_ping=True,
            pool_size=settings.database_pool_size,
            max_overflow=settings.database_max_overflow,
            pool_timeout=settings.database_pool_timeout,
            connect_args={"connect_timeout": 15},
        )
    return _engine


def get_session_factory():
    global _session_factory, _schema_ensured_on_connect
    if _session_factory is None:
        eng = get_engine()
        if eng is None:
            return None
        # expire_on_commit=False: mid-job commits (e.g. worker marks "processing") must not
        # expire the ORM instance before the final UPDATE (time/cost/usage).
        _session_factory = sessionmaker(
            bind=eng, autoflush=False, autocommit=False, expire_on_commit=False
        )
        if not _schema_ensured_on_connect:
            _schema_ensured_on_connect = True
            try:
                from app.db.schema_ensure import (
                    ensure_credit_transactions_table,
                    ensure_jobs_columns,
                    ensure_profiles_columns,
                    ensure_referral_attribution_columns,
                    ensure_transactions_columns,
                    ensure_usage_columns,
                )

                ensure_profiles_columns(eng)
                ensure_jobs_columns(eng)
                ensure_usage_columns(eng)
                ensure_referral_attribution_columns(eng)
                ensure_transactions_columns(eng)
                ensure_credit_transactions_table(eng)
            except Exception as e:
                logger.warning("Database schema ensure (non-fatal): %s", e)
    return _session_factory


def create_all_tables() -> None:
    global _schema_ensured_on_connect
    eng = get_engine()
    if eng is None:
        return
    from app.db import models  # noqa: F401 — register mappers

    Base.metadata.create_all(bind=eng)
    from app.db.schema_ensure import (
        ensure_credit_transactions_table,
        ensure_jobs_columns,
        ensure_profiles_columns,
        ensure_referral_attribution_columns,
        ensure_transactions_columns,
        ensure_usage_columns,
    )

    ensure_profiles_columns(eng)
    ensure_jobs_columns(eng)
    ensure_usage_columns(eng)
    ensure_referral_attribution_columns(eng)
    ensure_transactions_columns(eng)
    ensure_credit_transactions_table(eng)
    _schema_ensured_on_connect = True


def get_db() -> Generator[Session, None, None]:
    factory = get_session_factory()
    if factory is None:
        raise RuntimeError("database_url is not configured")
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
