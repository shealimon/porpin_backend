"""
Integration tests for public schema tables on PostgreSQL (Supabase).

There is no separate ``translations`` table: async document work is stored in
``public.jobs``; legacy API-key flow uses ``public.translation_jobs``.
Subscription state lives on ``public.profiles`` (no ``subscriptions`` table).

Requires ``SUPABASE_DATABASE_URL`` or ``DATABASE_URL`` and permission to insert
into ``auth.users`` for profile-scoped tests.
"""

from __future__ import annotations

import uuid

import psycopg2
import pytest
from psycopg2 import errors as pg_errors

from tests.conftest import (
    fetch_legacy_user,
    fetch_profile,
    insert_auth_user,
    insert_legacy_api_user,
)

pytestmark = pytest.mark.integration


def test_users_insert_select(dbcur, db):
    h = f"hash-{uuid.uuid4().hex}"
    uid = insert_legacy_api_user(dbcur, h)
    db.commit()

    row = fetch_legacy_user(dbcur, uid)
    assert row is not None
    assert row["api_key_hash"] == h
    assert row["tier"] == "free"
    assert row["email"] == f"legacy-{uid}@test.invalid"


def test_translation_jobs_user_relationship(dbcur, db):
    """Legacy ``users`` → ``translation_jobs`` (maps to product "translations" for API-key users)."""
    uid = insert_legacy_api_user(dbcur, f"hk-{uuid.uuid4().hex}")
    jid = uuid.uuid4()
    dbcur.execute(
        """
        INSERT INTO public.translation_jobs (
          id, user_id, status, input_filename, input_relpath, export_format
        )
        VALUES (%s, %s, 'pending', 'in.docx', 'uploads/in.docx', 'docx')
        """,
        (str(jid), str(uid)),
    )
    db.commit()

    dbcur.execute(
        """
        SELECT t.id, t.user_id, t.status, u.api_key_hash
        FROM public.translation_jobs t
        JOIN public.users u ON u.id = t.user_id
        WHERE t.id = %s
        """,
        (str(jid),),
    )
    row = dbcur.fetchone()
    assert row is not None
    assert row["user_id"] == uid
    assert row["status"] == "pending"


def test_profile_subscription_fields_and_jobs(dbcur, db):
    """``profiles`` holds subscription flags; ``jobs`` reference the same user id."""
    uid = insert_auth_user(dbcur)
    db.commit()
    prof = fetch_profile(dbcur, uid)
    assert prof is not None
    assert prof["subscription_active"] is False

    dbcur.execute(
        """
        UPDATE public.profiles
        SET subscription_active = TRUE,
            subscription_credits = 5000,
            plan = 'monthly'
        WHERE id = %s
        """,
        (str(uid),),
    )
    job_id = uuid.uuid4()
    dbcur.execute(
        """
        INSERT INTO public.jobs (
          id, user_id, status, input_filename, export_format, tokens_used, cost_inr
        )
        VALUES (%s, %s, 'completed', 'report.pdf', 'pdf', 100, 1.25)
        """,
        (str(job_id), str(uid)),
    )
    db.commit()

    dbcur.execute(
        """
        SELECT p.subscription_active, p.subscription_credits, p.plan, j.status
        FROM public.profiles p
        JOIN public.jobs j ON j.user_id = p.id
        WHERE p.id = %s AND j.id = %s
        """,
        (str(uid), str(job_id)),
    )
    row = dbcur.fetchone()
    assert row["subscription_active"] is True
    assert row["subscription_credits"] == 5000
    assert row["plan"] == "monthly"
    assert row["status"] == "completed"


def test_referral_linkage_profiles_and_attribution(dbcur, db):
    referrer_id = insert_auth_user(dbcur, "-r")
    referee_id = insert_auth_user(dbcur, "-f")
    db.commit()

    dbcur.execute(
        "UPDATE public.profiles SET referral_code = %s WHERE id = %s",
        (f"REF{referrer_id.hex[:8]}", str(referrer_id)),
    )
    dbcur.execute(
        """
        UPDATE public.profiles
        SET referred_by_user_id = %s
        WHERE id = %s
        """,
        (str(referrer_id), str(referee_id)),
    )
    rid = uuid.uuid4()
    dbcur.execute(
        """
        INSERT INTO public.referral_attributions (
          id, referee_user_id, referrer_user_id, status, words_credited_to_referrer
        )
        VALUES (%s, %s, %s, 'pending', 0)
        """,
        (str(rid), str(referee_id), str(referrer_id)),
    )
    db.commit()

    dbcur.execute(
        """
        SELECT ra.referee_user_id, ra.referrer_user_id, p.referred_by_user_id, p2.referral_code
        FROM public.referral_attributions ra
        JOIN public.profiles p ON p.id = ra.referee_user_id
        JOIN public.profiles p2 ON p2.id = ra.referrer_user_id
        WHERE ra.id = %s
        """,
        (str(rid),),
    )
    row = dbcur.fetchone()
    assert row["referee_user_id"] == referee_id
    assert row["referrer_user_id"] == referrer_id
    assert row["referred_by_user_id"] == referrer_id
    assert row["referral_code"] == f"REF{referrer_id.hex[:8]}"


def test_credit_transactions_roundtrip(dbcur, db):
    uid = insert_auth_user(dbcur, "-c")
    db.commit()
    cid = uuid.uuid4()
    key = f"idemp-{uuid.uuid4().hex}"
    dbcur.execute(
        """
        INSERT INTO public.credit_transactions (
          id, user_id, type, credits, idempotency_key
        )
        VALUES (%s, %s, 'referral_bonus', 42, %s)
        """,
        (str(cid), str(uid), key),
    )
    db.commit()

    dbcur.execute("SELECT * FROM public.credit_transactions WHERE id = %s", (str(cid),))
    row = dbcur.fetchone()
    assert row["credits"] == 42
    assert row["type"] == "referral_bonus"
    assert row["idempotency_key"] == key


def test_usage_and_payment_transactions_roundtrip(dbcur, db):
    uid = insert_auth_user(dbcur, "-u")
    db.commit()
    job_id = uuid.uuid4()
    dbcur.execute(
        """
        INSERT INTO public.jobs (id, user_id, status, input_filename)
        VALUES (%s, %s, 'completed', 'x.docx')
        """,
        (str(job_id), str(uid)),
    )
    usage_id = uuid.uuid4()
    dbcur.execute(
        """
        INSERT INTO public.usage (id, user_id, job_id, tokens_used, cost_inr)
        VALUES (%s, %s, %s, 10, 0.5)
        """,
        (str(usage_id), str(uid), str(job_id)),
    )
    tx_id = uuid.uuid4()
    dbcur.execute(
        """
        INSERT INTO public.transactions (
          id, user_id, amount_inr, provider, external_id, status
        )
        VALUES (%s, %s, 99.99, 'stripe', 'ch_test_1', 'completed')
        """,
        (str(tx_id), str(uid)),
    )
    db.commit()

    dbcur.execute("SELECT job_id, tokens_used FROM public.usage WHERE id = %s", (str(usage_id),))
    u = dbcur.fetchone()
    assert u["job_id"] == job_id
    assert u["tokens_used"] == 10

    dbcur.execute("SELECT amount_inr, provider FROM public.transactions WHERE id = %s", (str(tx_id),))
    t = dbcur.fetchone()
    assert float(t["amount_inr"]) == pytest.approx(99.99)
    assert t["provider"] == "stripe"


def test_duplicate_api_key_rejected(dbcur, db):
    h = f"dup-{uuid.uuid4().hex}"
    insert_legacy_api_user(dbcur, h)
    db.commit()
    with pytest.raises(pg_errors.UniqueViolation):
        insert_legacy_api_user(dbcur, h)
        db.commit()
    db.rollback()


def test_duplicate_referral_referee_rejected(dbcur, db):
    a = insert_auth_user(dbcur, "-a")
    b = insert_auth_user(dbcur, "-b")
    c = insert_auth_user(dbcur, "-c")
    db.commit()
    dbcur.execute(
        """
        INSERT INTO public.referral_attributions (referee_user_id, referrer_user_id)
        VALUES (%s, %s)
        """,
        (str(b), str(a)),
    )
    db.commit()
    with pytest.raises(pg_errors.UniqueViolation):
        dbcur.execute(
            """
            INSERT INTO public.referral_attributions (referee_user_id, referrer_user_id)
            VALUES (%s, %s)
            """,
            (str(b), str(c)),
        )
        db.commit()
    db.rollback()


def test_duplicate_credit_idempotency_key_rejected(dbcur, db):
    uid = insert_auth_user(dbcur, "-d")
    db.commit()
    key = f"same-{uuid.uuid4().hex}"
    dbcur.execute(
        """
        INSERT INTO public.credit_transactions (user_id, type, credits, idempotency_key)
        VALUES (%s, 'adj', 1, %s)
        """,
        (str(uid), key),
    )
    db.commit()
    with pytest.raises(pg_errors.UniqueViolation):
        dbcur.execute(
            """
            INSERT INTO public.credit_transactions (user_id, type, credits, idempotency_key)
            VALUES (%s, 'adj', 2, %s)
            """,
            (str(uid), key),
        )
        db.commit()
    db.rollback()


def test_not_null_users_api_key(dbcur, db):
    with pytest.raises(pg_errors.NotNullViolation):
        dbcur.execute(
            """
            INSERT INTO public.users (api_key_hash) VALUES (NULL)
            """
        )
        db.commit()
    db.rollback()


def test_referral_self_attribution_check_constraint(dbcur, db):
    uid = insert_auth_user(dbcur, "-selfref")
    db.commit()
    with pytest.raises(pg_errors.CheckViolation):
        dbcur.execute(
            """
            INSERT INTO public.referral_attributions (referee_user_id, referrer_user_id)
            VALUES (%s, %s)
            """,
            (str(uid), str(uid)),
        )
        db.commit()
    db.rollback()


def test_profile_self_referral_check_constraint(dbcur, db):
    uid = insert_auth_user(dbcur, "-selfp")
    db.commit()
    with pytest.raises(pg_errors.CheckViolation):
        dbcur.execute(
            """
            UPDATE public.profiles SET referred_by_user_id = %s WHERE id = %s
            """,
            (str(uid), str(uid)),
        )
        db.commit()
    db.rollback()


def test_invalid_fk_translation_jobs_user(dbcur, db):
    with pytest.raises(pg_errors.ForeignKeyViolation):
        dbcur.execute(
            """
            INSERT INTO public.translation_jobs (
              user_id, status, input_filename, input_relpath
            )
            VALUES (%s, 'pending', 'a.docx', 'a.docx')
            """,
            (str(uuid.uuid4()),),
        )
        db.commit()
    db.rollback()


def test_invalid_fk_jobs_profile(dbcur, db):
    with pytest.raises(pg_errors.ForeignKeyViolation):
        dbcur.execute(
            """
            INSERT INTO public.jobs (user_id, status, input_filename)
            VALUES (%s, 'pending', 'a.docx')
            """,
            (str(uuid.uuid4()),),
        )
        db.commit()
    db.rollback()


def test_transaction_rollback_on_failure(database_url):
    """After a failed statement, rollback leaves no trace of prior inserts in that txn."""
    bogus_user = uuid.uuid4()
    good_hash = f"rb-{uuid.uuid4().hex}"
    conn = psycopg2.connect(database_url)
    try:
        conn.autocommit = False
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO public.users (id, api_key_hash, tier)
            VALUES (%s, %s, 'free')
            """,
            (str(bogus_user), good_hash),
        )
        try:
            cur.execute(
                """
                INSERT INTO public.translation_jobs (
                  user_id, status, input_filename, input_relpath
                )
                VALUES (%s, 'pending', 'a.docx', 'a.docx')
                """,
                (str(uuid.uuid4()),),
            )
            conn.commit()
        except pg_errors.ForeignKeyViolation:
            conn.rollback()
        cur.close()

        conn2 = psycopg2.connect(database_url)
        try:
            c2 = conn2.cursor()
            c2.execute("SELECT 1 FROM public.users WHERE id = %s", (str(bogus_user),))
            assert c2.fetchone() is None
            c2.execute("SELECT 1 FROM public.users WHERE api_key_hash = %s", (good_hash,))
            assert c2.fetchone() is None
        finally:
            conn2.close()
    finally:
        conn.close()
