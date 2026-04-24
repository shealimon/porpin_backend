"""Unit tests: referral claim, rewards, anti-abuse outcomes, and idempotency (SQLite).

These run without PostgreSQL. Integration constraints (CHECK on self-referral rows)
are covered in ``test_supabase_schema_integration.py``.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import Session, sessionmaker

from app.core.pipeline_settings import get_pipeline_settings
from app.db import models  # noqa: F401 — register metadata
from app.db.base import Base
from app.db.models import CreditTransaction, PaymentTransaction, Profile, ReferralAttribution
from app.services import referral_lifecycle
from app.services.referrals import claim_referral, insert_credit_transaction_if_absent


@pytest.fixture
def pipeline_settings_small(monkeypatch: pytest.MonkeyPatch) -> None:
    get_pipeline_settings.cache_clear()
    monkeypatch.setenv("REFERRAL_REFEREE_SIGNUP_BONUS_WORDS", "500")
    monkeypatch.setenv("REFERRAL_REFERRER_VERIFY_REWARD_WORDS", "100")
    monkeypatch.setenv("REFERRAL_REFERRER_FIRST_PAYMENT_REWARD_WORDS", "200")
    monkeypatch.setenv("REFERRAL_MAX_REWARDED_REFERRALS", "20")
    monkeypatch.setenv("REFERRAL_MAX_WORDS_EARNED_PER_REFERRER", "50000")
    get_pipeline_settings.cache_clear()
    yield
    get_pipeline_settings.cache_clear()


@pytest.fixture
def session_factory(pipeline_settings_small: None):
    engine = create_engine("sqlite:///:memory:", future=True)

    @event.listens_for(engine, "connect")
    def _fk(dbapi_conn, _):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, future=True)


def _profile(
    session: Session,
    *,
    email: str,
    referral_code: str | None = None,
    free_credits: int = 0,
) -> Profile:
    p = Profile(
        id=uuid.uuid4(),
        email=email,
        referral_code=referral_code,
        free_credits=free_credits,
    )
    session.add(p)
    session.flush()
    return p


def test_user_a_refers_user_b_signup_recorded_and_referee_bonus(session_factory):
    with session_factory() as session:
        a = _profile(session, email="a@example.com", referral_code="abc123xyz")
        b = _profile(session, email="b@example.com", free_credits=1000)
        session.commit()
        aid, bid = a.id, b.id

    with session_factory() as session:
        b = session.get(Profile, bid)
        assert b is not None
        outcome, _ = claim_referral(session, bid, "ABC123xyz", claim_ip="203.0.113.1")
        assert outcome == "credited"
        session.commit()

    with session_factory() as session:
        b = session.get(Profile, bid)
        a = session.get(Profile, aid)
        assert b is not None and a is not None
        assert b.referred_by_user_id == a.id
        bonus = max(0, int(get_pipeline_settings().referral_referee_signup_bonus_words))
        assert int(b.free_credits) == 1000 + bonus

        attr = session.scalar(
            select(ReferralAttribution).where(ReferralAttribution.referee_user_id == bid)
        )
        assert attr is not None
        assert attr.referrer_user_id == a.id
        assert attr.status == "pending"

        ct = session.scalar(
            select(CreditTransaction).where(CreditTransaction.user_id == bid)
        )
        assert ct is not None
        assert ct.type == "referral_signup_bonus"
        assert int(ct.credits) == bonus
        assert ct.idempotency_key == f"referee_signup_bonus:{attr.id}"


def test_invalid_referral_code(session_factory):
    with session_factory() as session:
        _profile(session, email="lonely@example.com", referral_code="hascode01")
        u = _profile(session, email="new@example.com", free_credits=0)
        session.commit()
        uid = u.id

    with session_factory() as session:
        assert claim_referral(session, uid, "nope_not_real")[0] == "invalid_code"
        session.rollback()


def test_self_referral_blocked(session_factory):
    with session_factory() as session:
        a = _profile(session, email="solo@example.com", referral_code="selfcode01")
        session.commit()
        aid = a.id

    with session_factory() as session:
        outcome, _ = claim_referral(session, aid, "selfcode01")
        assert outcome == "self_referral"
        session.rollback()


def test_duplicate_claim_idempotent_no_double_bonus(session_factory):
    with session_factory() as session:
        _profile(session, email="ar@example.com", referral_code="dupcode001")
        b = _profile(session, email="br@example.com", free_credits=0)
        session.commit()
        bid = b.id

    bonus = max(0, int(get_pipeline_settings().referral_referee_signup_bonus_words))

    with session_factory() as session:
        assert claim_referral(session, bid, "dupcode001")[0] == "credited"
        session.commit()

    with session_factory() as session:
        assert claim_referral(session, bid, "dupcode001")[0] == "already_attributed"
        session.rollback()

    with session_factory() as session:
        b2 = session.get(Profile, bid)
        assert b2 is not None
        assert int(b2.free_credits) == bonus
        n = session.scalar(
            select(func.count()).select_from(CreditTransaction).where(CreditTransaction.user_id == bid)
        )
        assert int(n or 0) == 1


def test_email_match_blocked(session_factory):
    with session_factory() as session:
        a = _profile(session, email="Same@Example.com", referral_code="emailblk01")
        b = _profile(session, email="same@example.com", free_credits=0)
        session.commit()
        bid = b.id

    with session_factory() as session:
        assert claim_referral(session, bid, "emailblk01")[0] == "email_blocked"
        session.rollback()


def test_device_reuse_blocked(session_factory):
    with session_factory() as session:
        _profile(session, email="d1@example.com", referral_code="devreuse01")
        b1 = _profile(session, email="b1@example.com", free_credits=0)
        b2 = _profile(session, email="b2@example.com", free_credits=0)
        session.commit()
        b1_id, b2_id = b1.id, b2.id

    dev = "stable-device-fingerprint-12345678"
    with session_factory() as session:
        assert claim_referral(session, b1_id, "devreuse01", device_id=dev)[0] == "credited"
        session.commit()

    with session_factory() as session:
        assert claim_referral(session, b2_id, "devreuse01", device_id=dev)[0] == "device_reused"
        session.rollback()


def test_referrer_verify_and_payment_rewards_idempotent(session_factory):
    with session_factory() as session:
        a = _profile(session, email="ref@example.com", referral_code="payflow01")
        b = _profile(session, email="payee@example.com", free_credits=0)
        session.commit()
        aid, bid = a.id, b.id

    with session_factory() as session:
        assert claim_referral(session, bid, "payflow01")[0] == "credited"
        session.commit()

    settings = get_pipeline_settings()
    v_w = max(0, int(settings.referral_referrer_verify_reward_words))
    p_w = max(0, int(settings.referral_referrer_first_payment_reward_words))

    with session_factory() as session:
        assert referral_lifecycle.try_referrer_verify_reward(session, bid) is True
        session.commit()

    with session_factory() as session:
        a = session.get(Profile, aid)
        assert a is not None
        assert int(a.referral_bonus_words or 0) == v_w
        assert int(a.referral_words_earned_total or 0) == v_w
        attr_id = session.scalar(select(ReferralAttribution.id).where(ReferralAttribution.referee_user_id == bid))
        key = f"referrer_reward_verify:{attr_id}"
        assert session.scalar(select(CreditTransaction.id).where(CreditTransaction.idempotency_key == key)) is not None

    with session_factory() as session:
        assert referral_lifecycle.try_referrer_verify_reward(session, bid) is False
        session.rollback()
        a = session.get(Profile, aid)
        assert a is not None
        assert int(a.referral_bonus_words or 0) == v_w

    with session_factory() as session:
        b = session.get(Profile, bid)
        assert b is not None
        b.subscription_started_at = datetime.now(timezone.utc)
        session.add(b)
        session.commit()

    with session_factory() as session:
        assert referral_lifecycle.try_referrer_payment_reward(session, bid) is True
        session.commit()

    with session_factory() as session:
        a = session.get(Profile, aid)
        attr = session.scalar(select(ReferralAttribution).where(ReferralAttribution.referee_user_id == bid))
        assert a is not None and attr is not None
        assert attr.status == "completed"
        assert int(attr.words_credited_to_referrer or 0) == v_w + p_w
        assert int(a.referral_bonus_words or 0) == v_w + p_w

    with session_factory() as session:
        assert referral_lifecycle.try_referrer_payment_reward(session, bid) is False
        session.rollback()
        a = session.get(Profile, aid)
        assert a is not None
        assert int(a.referral_bonus_words or 0) == v_w + p_w


def test_payg_checkout_qualifies_first_payment(session_factory):
    with session_factory() as session:
        a = _profile(session, email="rw@example.com", referral_code="payg01")
        b = _profile(session, email="payer@example.com", free_credits=0)
        session.commit()
        aid, bid = a.id, b.id

    with session_factory() as session:
        claim_referral(session, bid, "payg01")
        referral_lifecycle.try_referrer_verify_reward(session, bid)
        session.commit()

    with session_factory() as session:
        session.add(
            PaymentTransaction(
                id=uuid.uuid4(),
                user_id=bid,
                amount_inr=99.0,
                provider="razorpay_wallet",
                external_id="ord_test",
                status="completed",
            )
        )
        session.commit()

    with session_factory() as session:
        assert referral_lifecycle.try_referrer_payment_reward(session, bid) is True
        session.commit()
        attr = session.scalar(select(ReferralAttribution).where(ReferralAttribution.referee_user_id == bid))
        assert attr is not None
        assert attr.status == "completed"


def test_insert_credit_transaction_if_absent_idempotent(session_factory):
    with session_factory() as session:
        p = _profile(session, email="led@example.com", free_credits=0)
        session.commit()
        pid = p.id

    with session_factory() as session:
        assert insert_credit_transaction_if_absent(
            session,
            user_id=pid,
            type_="test_grant",
            credits=5,
            referral_attribution_id=None,
            idempotency_key="idem-key-1",
        )
        assert not insert_credit_transaction_if_absent(
            session,
            user_id=pid,
            type_="test_grant",
            credits=999,
            referral_attribution_id=None,
            idempotency_key="idem-key-1",
        )
        session.commit()

    with session_factory() as session:
        n = session.scalar(
            select(func.count()).select_from(CreditTransaction).where(CreditTransaction.user_id == pid)
        )
        assert int(n or 0) == 1
        c = session.scalar(select(CreditTransaction).where(CreditTransaction.user_id == pid))
        assert c is not None
        assert int(c.credits) == 5
