"""Idempotent INR credit on profile after Razorpay payment capture (per-job PAYG)."""

from __future__ import annotations

import uuid
from decimal import Decimal

from fastapi import HTTPException
from sqlalchemy import select

from app.db.models import PaymentTransaction, Profile
from app.db.session import get_session_factory
from app.services.referral_lifecycle import try_referrer_payout_after_referee_event


def credit_inr_from_razorpay(
    *,
    profile_id: uuid.UUID,
    payment_id: str,
    amount_inr: float,
    kind: str = "payg_translation",
    razorpay_order_id: str | None = None,
    job_id: uuid.UUID | None = None,
) -> bool:
    """Idempotent credit (one row per Razorpay payment id). Returns True if a new credit was applied."""
    factory = get_session_factory()
    if factory is None:
        raise HTTPException(status_code=503, detail="Database is not configured.")
    amt = Decimal(str(round(max(0.0, amount_inr), 2)))
    if amt <= 0:
        return False
    with factory() as session:
        exists = session.scalar(
            select(PaymentTransaction).where(
                PaymentTransaction.external_id == payment_id
            )
        )
        if exists is not None:
            return False
        row = session.get(Profile, profile_id)
        if row is None:
            raise HTTPException(status_code=404, detail="Profile not found.")
        bal = Decimal(str(row.credits_inr_balance or 0))
        row.credits_inr_balance = float(bal + amt)
        session.add(
            PaymentTransaction(
                user_id=profile_id,
                amount_inr=float(amt),
                provider="razorpay_wallet",
                external_id=payment_id,
                status="completed",
                kind=kind,
                razorpay_order_id=razorpay_order_id,
                job_id=job_id,
            )
        )
        session.commit()
    try_referrer_payout_after_referee_event(profile_id)
    return True
