"""Billing: Razorpay webhook handling, subscription activation, PAYG credit idempotency, limits."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker

from app.billing_constants import PLAN_FREE, PLAN_MONTHLY, PLAN_PAYG, PLAN_YEARLY
from app.db import models  # noqa: F401
from app.db.base import Base
from app.db.models import PaymentTransaction, Profile
from app.services import razorpay_webhook
from app.services import profile_inr_credit as profile_inr_credit_mod
from app.services.razorpay_webhook import (
    extract_subscription_id_and_entity,
    process_razorpay_webhook_dict,
    webhook_dedup_key,
)
from app.services.word_credits import compute_word_charge, refresh_subscription_expiry


@pytest.fixture
def session_factory(monkeypatch: pytest.MonkeyPatch):
    engine = create_engine("sqlite:///:memory:", future=True)

    @event.listens_for(engine, "connect")
    def _fk(dbapi_conn, _):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, future=True)

    def _get_factory():
        return factory

    monkeypatch.setattr(razorpay_webhook, "get_session_factory", _get_factory)
    monkeypatch.setattr(profile_inr_credit_mod, "get_session_factory", _get_factory)
    yield factory


def _profile(session, **kwargs) -> Profile:
    defaults = dict(
        id=uuid.uuid4(),
        email="u@example.com",
        plan="free",
        free_credits=100,
        razorpay_subscription_id="sub_test123",
        pending_subscription_kind="monthly",
    )
    defaults.update(kwargs)
    p = Profile(**defaults)
    session.add(p)
    session.commit()
    session.refresh(p)
    return p


def test_subscription_activated_sets_monthly_plan_and_pool(session_factory):
    with session_factory() as session:
        p = _profile(session)

    ts_end = int(datetime.now(timezone.utc).timestamp()) + 86400 * 30
    payload = {
        "event": "subscription.activated",
        "id": "evt_unique_1",
        "payload": {
            "subscription": {
                "entity": {
                    "id": "sub_test123",
                    "current_start": int(datetime.now(timezone.utc).timestamp()),
                    "current_end": ts_end,
                }
            }
        },
    }
    process_razorpay_webhook_dict(payload)

    with session_factory() as session:
        row = session.get(Profile, p.id)
        assert row is not None
        assert row.plan == PLAN_MONTHLY
        assert row.subscription_active is True
        assert row.subscription_credits == 2_000_000
        assert row.pending_subscription_kind is None
        assert row.subscription_expiry is not None


def test_invoice_paid_resolves_subscription_from_invoice_entity(session_factory):
    with session_factory() as session:
        p = _profile(session, razorpay_subscription_id="sub_inv_only")

    ts = int(datetime.now(timezone.utc).timestamp())
    payload = {
        "event": "invoice.paid",
        "id": "evt_inv_1",
        "payload": {
            "invoice": {
                "entity": {
                    "id": "inv_xxx",
                    "subscription_id": "sub_inv_only",
                    "paid_at": ts,
                }
            }
        },
    }
    process_razorpay_webhook_dict(payload)

    with session_factory() as session:
        row = session.get(Profile, p.id)
        assert row is not None
        assert row.subscription_active is True
        assert row.plan == PLAN_MONTHLY


def test_duplicate_webhook_skips_second_apply(session_factory):
    with session_factory() as session:
        p = _profile(session)

    base = {
        "event": "subscription.charged",
        "id": "evt_dup_test",
        "payload": {
            "subscription": {
                "entity": {
                    "id": "sub_test123",
                    "current_end": int(datetime.now(timezone.utc).timestamp()) + 86400,
                }
            }
        },
    }
    process_razorpay_webhook_dict(base)
    process_razorpay_webhook_dict(base)

    with session_factory() as session:
        rows = session.scalars(
            select(PaymentTransaction).where(
                PaymentTransaction.external_id == "evt_dup_test",
                PaymentTransaction.provider == razorpay_webhook.WEBHOOK_TX_PROVIDER,
            )
        ).all()
        assert len(rows) == 1


def test_payment_failed_logged(caplog: pytest.LogCaptureFixture, session_factory):
    with session_factory() as session:
        _profile(session)

    caplog.set_level("WARNING")
    process_razorpay_webhook_dict(
        {
            "event": "payment.failed",
            "payload": {
                "payment": {
                    "entity": {
                        "id": "pay_bad",
                        "subscription_id": "sub_test123",
                        "error_code": "BAD_REQUEST",
                        "error_description": "card declined",
                    }
                }
            },
        }
    )
    assert "payment.failed" in caplog.text or "razorpay webhook failure" in caplog.text


def test_expired_subscription_stops_sub_pool(session_factory):
    past = datetime.now(timezone.utc) - timedelta(days=1)
    with session_factory() as session:
        p = Profile(
            id=uuid.uuid4(),
            email="x@example.com",
            plan=PLAN_MONTHLY,
            free_credits=0,
            referral_bonus_words=0,
            subscription_active=True,
            subscription_credits=100,
            subscription_expiry=past,
            razorpay_subscription_id="sub_x",
        )
        session.add(p)
        session.commit()
        pid = p.id

    with session_factory() as session:
        prof = session.get(Profile, pid)
        assert prof is not None
        refresh_subscription_expiry(prof, now=datetime.now(timezone.utc))
        session.commit()

    with session_factory() as session:
        prof = session.get(Profile, pid)
        assert prof is not None
        b = compute_word_charge(prof, 50_000, now=datetime.now(timezone.utc))
        assert b.subscription_used == 0
        assert b.payg_words > 0


def test_payg_inr_credit_idempotent_by_payment_id(session_factory):
    """Second apply with same Razorpay payment id does not double balance."""
    pid = uuid.uuid4()
    job_id = uuid.uuid4()
    with session_factory() as session:
        session.add(
            Profile(
                id=pid,
                email="w@example.com",
                plan=PLAN_FREE,
                free_credits=1000,
                credits_inr_balance=0.0,
            )
        )
        session.commit()

    from app.services.profile_inr_credit import credit_inr_from_razorpay

    assert (
        credit_inr_from_razorpay(
            profile_id=pid,
            payment_id="pay_same",
            amount_inr=100.0,
            kind="payg_translation",
            job_id=job_id,
        )
        is True
    )
    assert (
        credit_inr_from_razorpay(
            profile_id=pid,
            payment_id="pay_same",
            amount_inr=100.0,
            kind="payg_translation",
            job_id=job_id,
        )
        is False
    )

    with session_factory() as session:
        row = session.get(Profile, pid)
        assert row is not None
        assert float(row.credits_inr_balance) == 100.0


def test_extract_subscription_id_invoice_and_subscription():
    inv_payload = {
        "payload": {
            "invoice": {"entity": {"id": "i1", "subscription_id": "sub_from_inv"}}
        }
    }
    sid, _ = extract_subscription_id_and_entity(inv_payload)
    assert sid == "sub_from_inv"

    sub_payload = {
        "payload": {
            "subscription": {
                "entity": {"id": "sub_direct", "current_end": 1234567890},
            }
        }
    }
    sid2, _ = extract_subscription_id_and_entity(sub_payload)
    assert sid2 == "sub_direct"


def test_webhook_dedup_key_prefers_event_root_id():
    p = {"id": "evt_root", "event": "x", "payload": {}}
    assert webhook_dedup_key(p, "x") == "evt_root"


def test_yearly_activation_via_pending_kind(session_factory):
    with session_factory() as session:
        p = _profile(session, pending_subscription_kind="yearly")

    payload = {
        "event": "subscription.activated",
        "id": "evt_yearly",
        "payload": {
            "subscription": {
                "entity": {
                    "id": "sub_test123",
                    "current_end": int(datetime.now(timezone.utc).timestamp()) + 86400 * 400,
                }
            }
        },
    }
    process_razorpay_webhook_dict(payload)

    with session_factory() as session:
        row = session.get(Profile, p.id)
        assert row is not None
        assert row.plan == PLAN_YEARLY
        assert row.subscription_contract_end is not None
