"""Idempotent column additions for existing databases (Postgres / SQLite)."""

from __future__ import annotations

import logging

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine

from app.billing_constants import FREE_CREDITS_INITIAL

logger = logging.getLogger(__name__)

# Columns expected by the app but often missing on hand-created Supabase ``jobs`` tables.
_PROFILES_COLUMNS: dict[str, str] = {
    "first_name": "VARCHAR(100)",
    "last_name": "VARCHAR(100)",
    "mobile": "VARCHAR(32)",
    "city": "VARCHAR(120)",
    "country": "VARCHAR(120)",
    "referral_code": "VARCHAR(32)",
    "referred_by_user_id": "UUID",
    "referral_bonus_words": "INTEGER NOT NULL DEFAULT 0",
    "referral_words_earned_total": "INTEGER NOT NULL DEFAULT 0",
    "free_credits": f"INTEGER NOT NULL DEFAULT {FREE_CREDITS_INITIAL}",
    "subscription_active": "BOOLEAN NOT NULL DEFAULT FALSE",
    "subscription_credits": "INTEGER NOT NULL DEFAULT 0",
    "subscription_expiry": "TIMESTAMP WITH TIME ZONE",
    "subscription_started_at": "TIMESTAMP WITH TIME ZONE",
    "subscription_period_start": "TIMESTAMP WITH TIME ZONE",
    "subscription_contract_end": "TIMESTAMP WITH TIME ZONE",
    "pending_subscription_kind": "VARCHAR(16)",
    "razorpay_subscription_id": "VARCHAR(255)",
}

_JOBS_COLUMNS: dict[str, str] = {
    "input_filename": "TEXT NOT NULL DEFAULT ''",
    "export_format": "TEXT NOT NULL DEFAULT 'docx'",
    "file_type": "TEXT",
    "content_hash": "VARCHAR(64)",
    "tokens_used": "BIGINT NOT NULL DEFAULT 0",
    "cost_inr": "NUMERIC(10, 2) NOT NULL DEFAULT 0",
    "processing_time_seconds": "DOUBLE PRECISION",
    "updated_at": "TIMESTAMP WITH TIME ZONE",
    "quoted_payg_inr": "NUMERIC(10, 2) NOT NULL DEFAULT 0",
    "translation_attempt": "INTEGER NOT NULL DEFAULT 0",
    "translation_target": "VARCHAR(16) NOT NULL DEFAULT 'hinglish'",
}

_USAGE_COLUMNS: dict[str, str] = {
    "job_id": "UUID REFERENCES public.jobs (id) ON DELETE SET NULL",
    "tokens_used": "INTEGER NOT NULL DEFAULT 0",
    "cost_inr": "NUMERIC(12, 4) NOT NULL DEFAULT 0",
}


def _sqlite_type(sql: str) -> str:
    """SQLite-friendly types."""
    s = sql
    if "UUID" in s:
        s = s.replace("UUID", "VARCHAR(36)")
    if "DOUBLE PRECISION" in s:
        s = s.replace("DOUBLE PRECISION", "REAL")
    if "NUMERIC" in s:
        s = (
            s.replace("NUMERIC(12, 4)", "REAL")
            .replace("NUMERIC(12,4)", "REAL")
            .replace("NUMERIC(10, 2)", "REAL")
            .replace("NUMERIC(10,2)", "REAL")
        )
    if "TIMESTAMP WITH TIME ZONE" in s:
        s = s.replace("TIMESTAMP WITH TIME ZONE", "TIMESTAMP")
    if "VARCHAR(64)" in s:
        s = s.replace("VARCHAR(64)", "VARCHAR(64)")
    return s


def ensure_profiles_columns(engine: Engine) -> None:
    """Add missing columns on ``profiles`` for upgrades / SQLite dev DBs."""
    insp = inspect(engine)
    if not insp.has_table("profiles"):
        return
    existing = {c["name"] for c in insp.get_columns("profiles")}
    dialect = engine.dialect.name
    with engine.begin() as conn:
        for col, ddl in _PROFILES_COLUMNS.items():
            if col in existing:
                continue
            typ = _sqlite_type(ddl) if dialect == "sqlite" else ddl
            try:
                conn.execute(text(f'ALTER TABLE profiles ADD COLUMN "{col}" {typ}'))
                logger.info("Added column profiles.%s", col)
            except Exception as e:
                logger.warning("Could not add column profiles.%s: %s", col, e)


def ensure_jobs_columns(engine: Engine) -> None:
    """Add missing columns on ``jobs`` for upgrades."""
    insp = inspect(engine)
    if not insp.has_table("jobs"):
        return
    existing = {c["name"] for c in insp.get_columns("jobs")}
    dialect = engine.dialect.name
    with engine.begin() as conn:
        for col, ddl in _JOBS_COLUMNS.items():
            if col in existing:
                continue
            typ = _sqlite_type(ddl) if dialect == "sqlite" else ddl
            try:
                conn.execute(text(f'ALTER TABLE jobs ADD COLUMN "{col}" {typ}'))
                logger.info("Added column jobs.%s", col)
            except Exception as e:
                logger.warning("Could not add column jobs.%s: %s", col, e)


_REFERRAL_ATTRIBUTION_COLUMNS: dict[str, str] = {
    "status": "VARCHAR(16) NOT NULL DEFAULT 'pending'",
    "claim_ip": "VARCHAR(45)",
    "device_hash": "VARCHAR(64)",
}


def ensure_referral_attribution_columns(engine: Engine) -> None:
    insp = inspect(engine)
    if not insp.has_table("referral_attributions"):
        return
    existing = {c["name"] for c in insp.get_columns("referral_attributions")}
    dialect = engine.dialect.name
    with engine.begin() as conn:
        for col, ddl in _REFERRAL_ATTRIBUTION_COLUMNS.items():
            if col in existing:
                continue
            typ = _sqlite_type(ddl) if dialect == "sqlite" else ddl
            try:
                conn.execute(
                    text(f'ALTER TABLE referral_attributions ADD COLUMN "{col}" {typ}')
                )
                logger.info("Added column referral_attributions.%s", col)
            except Exception as e:
                logger.warning("Could not add column referral_attributions.%s: %s", col, e)
        try:
            conn.execute(
                text(
                    "UPDATE referral_attributions SET status = 'completed' "
                    "WHERE COALESCE(words_credited_to_referrer, 0) > 0 "
                    "AND (status IS NULL OR status = '' OR status = 'pending')"
                )
            )
        except Exception as e:
            logger.warning("referral_attributions status backfill: %s", e)


def ensure_credit_transactions_table(engine: Engine) -> None:
    """Create ledger table if missing (SQLAlchemy metadata)."""
    insp = inspect(engine)
    if insp.has_table("credit_transactions"):
        return
    try:
        from app.db.models import CreditTransaction  # noqa: PLC0415

        CreditTransaction.__table__.create(bind=engine, checkfirst=True)
        logger.info("Created table credit_transactions")
    except Exception as e:
        logger.warning("Could not create credit_transactions: %s", e)


_TRANSACTIONS_COLUMNS: dict[str, str] = {
    "kind": "VARCHAR(32) NOT NULL DEFAULT 'wallet_topup'",
    "razorpay_order_id": "VARCHAR(255)",
    "job_id": "UUID REFERENCES public.jobs (id) ON DELETE SET NULL",
}


def ensure_transactions_columns(engine: Engine) -> None:
    """Add missing columns on ``transactions`` (PAYG job links, payment kind)."""
    insp = inspect(engine)
    if not insp.has_table("transactions"):
        return
    existing = {c["name"] for c in insp.get_columns("transactions")}
    dialect = engine.dialect.name
    with engine.begin() as conn:
        for col, ddl in _TRANSACTIONS_COLUMNS.items():
            if col in existing:
                continue
            typ = _sqlite_type(ddl) if dialect == "sqlite" else ddl
            if dialect == "sqlite" and "REFERENCES" in typ.upper():
                typ = "VARCHAR(36)"
            try:
                conn.execute(text(f'ALTER TABLE transactions ADD COLUMN "{col}" {typ}'))
                logger.info("Added column transactions.%s", col)
            except Exception as e:
                logger.warning("Could not add column transactions.%s: %s", col, e)


def ensure_usage_columns(engine: Engine) -> None:
    """Add missing columns on ``usage`` (Supabase / older alembic baselines)."""
    insp = inspect(engine)
    if not insp.has_table("usage"):
        return
    existing = {c["name"] for c in insp.get_columns("usage")}
    dialect = engine.dialect.name
    with engine.begin() as conn:
        for col, ddl in _USAGE_COLUMNS.items():
            if col in existing:
                continue
            typ = _sqlite_type(ddl) if dialect == "sqlite" else ddl
            if dialect == "sqlite" and "REFERENCES" in typ.upper():
                typ = typ.split("REFERENCES")[0].strip()
            try:
                conn.execute(text(f'ALTER TABLE usage ADD COLUMN "{col}" {typ}'))
                logger.info("Added column usage.%s", col)
            except Exception as e:
                logger.warning("Could not add column usage.%s: %s", col, e)


def ensure_updated_at_server_default(engine: Engine) -> None:
    """Best-effort: SQLite cannot add server default in one step; app sets updated_at."""
    return
