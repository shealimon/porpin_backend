"""Referral stages: email verification (referrer step 1), first payment (referrer step 2)."""

from __future__ import annotations

import logging
import uuid

from sqlalchemy import func, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.pipeline_settings import get_pipeline_settings
from app.db.models import CreditTransaction, PaymentTransaction, Profile, ReferralAttribution
from app.db.session import get_session_factory
from app.services.referrals import insert_credit_transaction_if_absent

logger = logging.getLogger(__name__)


def auth_user_email_confirmed(session: Session, user_id: uuid.UUID) -> bool:
    """Reads Supabase auth.users.email_confirmed_at; treats missing auth schema as confirmed (local SQLite)."""
    try:
        row = session.execute(
            text(
                "SELECT email_confirmed_at IS NOT NULL AS ok "
                "FROM auth.users WHERE id = CAST(:id AS uuid) LIMIT 1"
            ),
            {"id": str(user_id)},
        ).first()
        if row is None:
            return False
        return bool(row[0])
    except SQLAlchemyError:
        return True


def advance_referee_referral_to_verified(session: Session, referee_id: uuid.UUID) -> bool:
    attr = session.scalar(
        select(ReferralAttribution).where(
            ReferralAttribution.referee_user_id == referee_id,
            ReferralAttribution.status == "pending",
        )
    )
    if attr is None:
        return False
    if not auth_user_email_confirmed(session, referee_id):
        return False
    attr.status = "verified"
    session.add(attr)
    return True


def referee_has_payg_checkout_payment(session: Session, referee_id: uuid.UUID) -> bool:
    q = select(func.count()).select_from(PaymentTransaction).where(
        PaymentTransaction.user_id == referee_id,
        PaymentTransaction.status == "completed",
        PaymentTransaction.provider == "razorpay_wallet",
        PaymentTransaction.amount_inr > 0,
    )
    return int(session.scalar(q) or 0) > 0


def referee_has_qualifying_first_payment(session: Session, referee_id: uuid.UUID) -> bool:
    """First spend: PayG (Razorpay) payment or any paid subscription activation."""
    if referee_has_payg_checkout_payment(session, referee_id):
        return True
    p = session.get(Profile, referee_id)
    if p is None:
        return False
    return p.subscription_started_at is not None


def _count_referrer_started_rewards(session: Session, referrer_id: uuid.UUID) -> int:
    """Attributions where the referrer has received any word credit."""
    q = select(func.count()).select_from(ReferralAttribution).where(
        ReferralAttribution.referrer_user_id == referrer_id,
        ReferralAttribution.words_credited_to_referrer > 0,
    )
    return int(session.scalar(q) or 0)


def _count_referrer_paid_completions(session: Session, referrer_id: uuid.UUID) -> int:
    q = select(func.count()).select_from(ReferralAttribution).where(
        ReferralAttribution.referrer_user_id == referrer_id,
        ReferralAttribution.status == "completed",
        ReferralAttribution.words_credited_to_referrer > 0,
    )
    return int(session.scalar(q) or 0)


def _apply_referrer_word_credit(
    session: Session,
    *,
    referrer: Profile,
    attr: ReferralAttribution,
    amount: int,
    type_: str,
    idempotency_key: str,
) -> bool:
    if amount <= 0:
        return False
    earned_total = max(0, int(referrer.referral_words_earned_total or 0))
    settings = get_pipeline_settings()
    cap_words = max(0, int(settings.referral_max_words_earned_per_referrer))
    room = cap_words - earned_total
    if room <= 0:
        return False
    grant = min(int(amount), room)
    if grant <= 0:
        return False
    if not insert_credit_transaction_if_absent(
        session,
        user_id=referrer.id,
        type_=type_,
        credits=grant,
        referral_attribution_id=attr.id,
        idempotency_key=idempotency_key,
    ):
        return False
    referrer.referral_bonus_words = int(referrer.referral_bonus_words or 0) + grant
    referrer.referral_words_earned_total = earned_total + grant
    attr.words_credited_to_referrer = int(attr.words_credited_to_referrer or 0) + grant
    session.add(referrer)
    session.add(attr)
    return True


def try_referrer_verify_reward(session: Session, referee_id: uuid.UUID) -> bool:
    """Credit referrer for verified email + login (step 1). Idempotent."""
    settings = get_pipeline_settings()
    verify_w = max(0, int(settings.referral_referrer_verify_reward_words))
    max_k = max(0, int(settings.referral_max_rewarded_referrals))

    attr = session.scalar(
        select(ReferralAttribution)
        .where(ReferralAttribution.referee_user_id == referee_id)
        .with_for_update(of=ReferralAttribution)
    )
    if attr is None:
        return False

    changed = False
    if attr.status == "pending" and auth_user_email_confirmed(session, referee_id):
        attr.status = "verified"
        session.add(attr)
        changed = True

    if attr.status != "verified":
        return changed

    if verify_w <= 0:
        return changed

    key = f"referrer_reward_verify:{attr.id}"
    exists_ct = session.scalar(select(CreditTransaction.id).where(CreditTransaction.idempotency_key == key))
    if exists_ct is not None:
        return changed

    already = int(attr.words_credited_to_referrer or 0)
    if already >= verify_w:
        return changed

    referrer = session.get(Profile, attr.referrer_user_id, with_for_update=True)
    if referrer is None:
        return changed

    started = _count_referrer_started_rewards(session, referrer.id)
    if started >= max_k:
        return changed

    if _apply_referrer_word_credit(
        session,
        referrer=referrer,
        attr=attr,
        amount=verify_w,
        type_="referral_reward_verify",
        idempotency_key=key,
    ):
        return True
    return changed


def try_referrer_payment_reward(session: Session, referee_id: uuid.UUID) -> bool:
    """After referee's first payment (PAYG / subscription), credit step 2 and complete. Idempotent."""
    settings = get_pipeline_settings()
    verify_w = max(0, int(settings.referral_referrer_verify_reward_words))
    pay_w = max(0, int(settings.referral_referrer_first_payment_reward_words))
    max_k = max(0, int(settings.referral_max_rewarded_referrals))
    total_target = verify_w + pay_w

    attr = session.scalar(
        select(ReferralAttribution)
        .where(ReferralAttribution.referee_user_id == referee_id)
        .with_for_update(of=ReferralAttribution)
    )
    if attr is None:
        return False
    if attr.status == "completed":
        return False

    changed = False
    if attr.status == "pending" and auth_user_email_confirmed(session, referee_id):
        attr.status = "verified"
        session.add(attr)
        changed = True

    if attr.status != "verified":
        return changed

    if not referee_has_qualifying_first_payment(session, referee_id):
        return changed

    already = int(attr.words_credited_to_referrer or 0)
    if total_target > 0 and already >= total_target:
        attr.status = "completed"
        session.add(attr)
        return True

    remainder = total_target - already if total_target > 0 else pay_w
    if remainder <= 0:
        attr.status = "completed"
        session.add(attr)
        return True

    referrer = session.get(Profile, attr.referrer_user_id, with_for_update=True)
    if referrer is None:
        return changed

    # No step-1 credit and all reward slots are used (e.g. 11th friend): do not pay a lump sum on payment.
    started = _count_referrer_started_rewards(session, referrer.id)
    if verify_w > 0 and already < verify_w and started >= max_k:
        attr.status = "completed"
        session.add(attr)
        return True

    completed_slots = _count_referrer_paid_completions(session, referrer.id)
    if completed_slots >= max_k:
        attr.status = "completed"
        session.add(attr)
        return True

    key = f"referrer_reward_payment:{attr.id}"
    exists_ct = session.scalar(select(CreditTransaction.id).where(CreditTransaction.idempotency_key == key))
    if exists_ct is not None:
        attr.status = "completed"
        session.add(attr)
        return True

    _apply_referrer_word_credit(
        session,
        referrer=referrer,
        attr=attr,
        amount=remainder,
        type_="referral_reward_payment",
        idempotency_key=key,
    )
    attr.status = "completed"
    session.add(attr)
    return True


def try_referrer_payout_for_referee(session: Session, referee_id: uuid.UUID) -> bool:
    """Advance verification rewards and payment rewards for this referee."""
    changed = False
    if try_referrer_verify_reward(session, referee_id):
        changed = True
    if try_referrer_payment_reward(session, referee_id):
        changed = True
    return changed


def sync_referral_lifecycle_for_profile_row(session: Session, profile: Profile) -> bool:
    """Run after profile/JWT sync: referral stages for this user as referee."""
    changed = False
    if advance_referee_referral_to_verified(session, profile.id):
        changed = True
    if try_referrer_payout_for_referee(session, profile.id):
        changed = True
    return changed


def try_referrer_payout_after_referee_event(referee_user_id: uuid.UUID) -> None:
    """Call after pay-as-you-go or subscription payment (new DB session)."""
    factory = get_session_factory()
    if factory is None:
        return
    try:
        with factory() as session:
            if try_referrer_payout_for_referee(session, referee_user_id):
                session.commit()
    except Exception:
        logger.exception("referral payout hook failed for referee %s", referee_user_id)


def referral_ui_message(*, status: str, payout_words: int) -> str:
    settings = get_pipeline_settings()
    v = max(0, int(settings.referral_referrer_verify_reward_words))
    p = max(0, int(settings.referral_referrer_first_payment_reward_words))
    total = v + p
    w = int(payout_words or 0)

    if status == "pending":
        return (
            f"Friend signed up. You earn {v:,} words when they verify email and sign in; "
            f"then {p:,} more when they pay (PAYG or subscription)—{total:,} words total."
        )
    if status == "verified":
        if w >= total:
            return f"+{total:,} words from this invite."
        if w >= v:
            return f"+{w:,} words added. +{p:,} words pending until their first payment."
        if w > 0:
            return f"+{w:,} words added. Remaining bonus pending."
        return (
            f"Friend verified. +{v:,} words could not be added (referral limit). "
            f"+{p:,} words still pending until they pay."
        )
    if status == "completed" and w > 0:
        return f"+{w:,} words added from this invite."
    if status == "completed":
        return "Completed — referral reward limit reached."
    return ""
