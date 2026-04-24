"""Referral attribution: invite link, referee signup bonus, staged referrer rewards."""

from __future__ import annotations

import hashlib
import secrets
import string
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.pipeline_settings import get_pipeline_settings
from app.db.models import CreditTransaction, Profile, ReferralAttribution


def generate_referral_code() -> str:
    alphabet = string.ascii_lowercase + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(10))


def ensure_referral_code(session: Session, profile: Profile) -> bool:
    """Assign referral_code if missing. Returns True if database was mutated."""
    if profile.referral_code:
        return False
    for _ in range(32):
        code = generate_referral_code()
        taken = session.scalar(select(Profile.id).where(Profile.referral_code == code))
        if taken is None:
            profile.referral_code = code
            return True
    raise RuntimeError("Could not allocate a unique referral code")


def normalize_referral_code(raw: str) -> str:
    return raw.strip().lower()


def hash_device_id(raw: str | None) -> str | None:
    if not raw:
        return None
    t = raw.strip()
    if len(t) < 8:
        return None
    return hashlib.sha256(t.encode("utf-8")).hexdigest()


def _norm_email(value: str | None) -> str | None:
    if not value:
        return None
    t = value.strip().lower()
    return t or None


def insert_credit_transaction_if_absent(
    session: Session,
    *,
    user_id: uuid.UUID,
    type_: str,
    credits: int,
    referral_attribution_id: uuid.UUID | None,
    idempotency_key: str,
) -> bool:
    exists = session.scalar(
        select(CreditTransaction.id).where(CreditTransaction.idempotency_key == idempotency_key)
    )
    if exists is not None:
        return False
    session.add(
        CreditTransaction(
            user_id=user_id,
            type=type_,
            credits=int(credits),
            referral_attribution_id=referral_attribution_id,
            idempotency_key=idempotency_key,
        )
    )
    return True


def _device_already_used(session: Session, device_hash: str, referee_id: uuid.UUID) -> bool:
    row = session.scalar(
        select(ReferralAttribution).where(
            ReferralAttribution.device_hash == device_hash,
            ReferralAttribution.referee_user_id != referee_id,
        )
    )
    return row is not None


def claim_referral(
    session: Session,
    referee_id: uuid.UUID,
    raw_code: str,
    *,
    claim_ip: str | None = None,
    device_id: str | None = None,
) -> tuple[str, int]:
    """Returns (outcome, words_credited_to_referrer on this call — always 0 until completion rules).

    outcome:
      credited — new attribution; referee received signup bonus words
      already_attributed — referee was already linked
      invalid_code — no matching referrer
      self_referral — same account as referrer
      email_blocked — referee email matches referrer
      device_reused — device fingerprint already used for another referee
    """
    code = normalize_referral_code(raw_code)
    if len(code) < 3:
        return ("invalid_code", 0)

    existing_attr = session.scalar(
        select(ReferralAttribution).where(ReferralAttribution.referee_user_id == referee_id)
    )
    if existing_attr is not None:
        return ("already_attributed", 0)

    referee = session.get(Profile, referee_id)
    if referee is None:
        return ("invalid_code", 0)

    if referee.referred_by_user_id is not None:
        return ("already_attributed", 0)

    referrer = session.scalar(select(Profile).where(Profile.referral_code == code))
    if referrer is None:
        return ("invalid_code", 0)
    if referrer.id == referee_id:
        return ("self_referral", 0)

    re = _norm_email(referee.email)
    rr = _norm_email(referrer.email)
    if re and rr and re == rr:
        return ("email_blocked", 0)

    device_hash = hash_device_id(device_id)
    if device_hash and _device_already_used(session, device_hash, referee_id):
        return ("device_reused", 0)

    settings = get_pipeline_settings()
    bonus = max(0, int(settings.referral_referee_signup_bonus_words))

    referee.referred_by_user_id = referrer.id
    referee.free_credits = int(referee.free_credits or 0) + bonus
    session.add(referee)

    attr = ReferralAttribution(
        referee_user_id=referee.id,
        referrer_user_id=referrer.id,
        status="pending",
        claim_ip=(claim_ip[:45] if claim_ip else None),
        device_hash=device_hash,
        words_credited_to_referrer=0,
    )
    session.add(attr)
    session.flush()

    if bonus > 0:
        insert_credit_transaction_if_absent(
            session,
            user_id=referee.id,
            type_="referral_signup_bonus",
            credits=bonus,
            referral_attribution_id=attr.id,
            idempotency_key=f"referee_signup_bonus:{attr.id}",
        )

    return ("credited", 0)
