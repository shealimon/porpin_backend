"""Fixtures for DB integration tests (Supabase / Postgres)."""

from __future__ import annotations

import os
import uuid
from collections.abc import Generator
from typing import Any

import psycopg2
import pytest
from psycopg2.extras import RealDictCursor


def _database_url() -> str | None:
    return os.environ.get("SUPABASE_DATABASE_URL") or os.environ.get("DATABASE_URL")


@pytest.fixture(scope="session")
def database_url() -> str:
    url = _database_url()
    if not url:
        pytest.skip(
            "Set SUPABASE_DATABASE_URL or DATABASE_URL to run integration DB tests."
        )
    return url


@pytest.fixture
def db(database_url: str) -> Generator[psycopg2.extensions.connection, None, None]:
    """One connection per test; always rolled back so the database stays clean."""
    conn = psycopg2.connect(database_url)
    conn.autocommit = False
    try:
        yield conn
    finally:
        conn.rollback()
        conn.close()


@pytest.fixture
def dbcur(
    db: psycopg2.extensions.connection,
) -> Generator[RealDictCursor, None, None]:
    cur = db.cursor(cursor_factory=RealDictCursor)
    try:
        yield cur
    finally:
        cur.close()


def insert_legacy_api_user(cur: RealDictCursor, api_key_hash: str) -> uuid.UUID:
    uid = uuid.uuid4()
    cur.execute(
        """
        INSERT INTO public.users (id, api_key_hash, tier, email)
        VALUES (%s, %s, 'free', %s)
        RETURNING id
        """,
        (str(uid), api_key_hash, f"legacy-{uid}@test.invalid"),
    )
    row = cur.fetchone()
    assert row is not None
    return uuid.UUID(str(row["id"]))


def insert_auth_user(cur: RealDictCursor, email_suffix: str = "") -> uuid.UUID:
    """Insert auth.users; trigger should create public.profiles. Requires DB privileges."""
    uid = uuid.uuid4()
    email = f"auth-{uid}{email_suffix}@test.invalid"
    try:
        cur.execute(
            """
            INSERT INTO auth.users (
              id, instance_id, aud, role, email, encrypted_password,
              raw_app_meta_data, raw_user_meta_data, created_at, updated_at
            )
            VALUES (
              %s, '00000000-0000-0000-0000-000000000000',
              'authenticated', 'authenticated', %s,
              crypt('pytest-db-secret', gen_salt('bf')),
              '{}', '{}', now(), now()
            )
            """,
            (str(uid), email),
        )
    except psycopg2.Error as e:
        pytest.skip(f"Cannot insert auth.users (need SQL access to auth schema): {e}")
    return uid


def fetch_profile(cur: RealDictCursor, profile_id: uuid.UUID) -> dict[str, Any] | None:
    cur.execute("SELECT * FROM public.profiles WHERE id = %s", (str(profile_id),))
    return cur.fetchone()


def fetch_legacy_user(cur: RealDictCursor, user_id: uuid.UUID) -> dict[str, Any] | None:
    cur.execute("SELECT * FROM public.users WHERE id = %s", (str(user_id),))
    return cur.fetchone()
