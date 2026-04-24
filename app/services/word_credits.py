"""Word buckets: free credits, subscription pool, pay-as-you-go (INR)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.billing_constants import (
    PLAN_FREE,
    PLAN_MONTHLY,
    PLAN_PAYG,
    PLAN_YEARLY,
)
from app.db.models import Profile, UsageRecord, User, UserTier
from app.payg_pricing import estimate_payg_inr

SUBSCRIPTION_CREDITS_MONTHLY = 2_000_000
SUBSCRIPTION_PERIOD_DAYS = 30
SUBSCRIPTION_YEAR_DAYS = 365


@dataclass(frozen=True)
class WordChargeBreakdown:
    total_words: int
    free_used: int
    subscription_used: int
    payg_words: int
    amount_to_pay: float
    remaining_words: int
    user_plan_type: str

    def as_dict(self) -> dict:
        return {
            "total_words": self.total_words,
            "free_used": self.free_used,
            "subscription_used": self.subscription_used,
            "remaining_words": self.remaining_words,
            "amount_to_pay": self.amount_to_pay,
            "user_plan_type": self.user_plan_type,
        }


def _ensure_aware_utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if getattr(dt, "tzinfo", None) is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def refresh_yearly_monthly_word_bucket(profile: Profile, now: datetime) -> None:
    """For yearly plans: reset 2M words every 30 days within the contract window."""
    if str(profile.plan).lower() != PLAN_YEARLY or not profile.subscription_active:
        return
    contract = profile.subscription_contract_end or profile.subscription_expiry
    contract = _ensure_aware_utc(contract)
    if contract is None:
        return
    if now >= contract:
        return
    ps = profile.subscription_period_start or profile.subscription_started_at
    ps = _ensure_aware_utc(ps)
    if ps is None:
        profile.subscription_period_start = now
        return

    month = timedelta(days=SUBSCRIPTION_PERIOD_DAYS)
    next_boundary = ps + month
    while now >= next_boundary and next_boundary <= contract:
        ps = next_boundary
        profile.subscription_credits = SUBSCRIPTION_CREDITS_MONTHLY
        next_boundary = ps + month
    profile.subscription_period_start = ps


def refresh_subscription_expiry(profile: Profile, now: datetime | None = None) -> None:
    """Deactivate expired subscriptions; roll yearly monthly word buckets."""
    now = now or datetime.now(timezone.utc)
    refresh_yearly_monthly_word_bucket(profile, now)

    exp = profile.subscription_expiry
    if exp is None:
        if profile.subscription_active:
            profile.subscription_active = False
            profile.subscription_credits = 0
            _plan_after_subscription_ends(profile)
        return
    exp = _ensure_aware_utc(exp)
    if profile.subscription_active and exp is not None and exp < now:
        profile.subscription_active = False
        profile.subscription_credits = 0
        _plan_after_subscription_ends(profile)


def _plan_after_subscription_ends(profile: Profile) -> None:
    if int(profile.free_credits or 0) <= 0:
        profile.plan = PLAN_PAYG
    else:
        profile.plan = PLAN_FREE


def sync_plan_after_free_pool_depleted(profile: Profile) -> None:
    """When signup free words hit 0, move marketing plan from free → pay-as-you-go."""
    if profile.subscription_active:
        return
    if int(profile.free_credits or 0) > 0:
        return
    if str(profile.plan).lower() == PLAN_FREE:
        profile.plan = PLAN_PAYG


def compute_word_charge(profile: Profile, total_words: int, *, now: datetime | None = None) -> WordChargeBreakdown:
    """Preview/settlement split: free (incl. referral bonus) → subscription → PAYG."""
    now = now or datetime.now(timezone.utc)
    refresh_subscription_expiry(profile, now)

    tw = max(0, int(total_words))
    need = tw

    fc = int(profile.free_credits or 0)
    rb = int(profile.referral_bonus_words or 0)

    use_fc = min(need, fc)
    need -= use_fc
    use_rb = min(need, rb)
    need -= use_rb
    free_used = use_fc + use_rb

    sub_left = int(profile.subscription_credits or 0)
    sub_active = bool(profile.subscription_active) and sub_left > 0
    if sub_active:
        exp = profile.subscription_expiry
        if exp is not None:
            exp = _ensure_aware_utc(exp)
            if exp is not None and exp < now:
                sub_active = False

    subscription_used = min(need, sub_left) if sub_active else 0
    need -= subscription_used

    payg_words = need
    amount_to_pay = estimate_payg_inr(payg_words) if payg_words else 0.0

    rem_free = fc - use_fc
    rem_rb = rb - use_rb
    rem_sub = (sub_left - subscription_used) if sub_active else 0
    remaining_words = max(0, rem_free + rem_rb + rem_sub)

    if payg_words > 0 and (free_used > 0 or subscription_used > 0):
        upt = "mixed"
    elif payg_words > 0:
        upt = "payg"
    elif subscription_used > 0:
        upt = "subscription"
    else:
        upt = "free"

    return WordChargeBreakdown(
        total_words=tw,
        free_used=free_used,
        subscription_used=subscription_used,
        payg_words=payg_words,
        amount_to_pay=amount_to_pay,
        remaining_words=remaining_words,
        user_plan_type=upt,
    )


def set_legacy_api_user_subscription_tier(
    session: Session, user_id: uuid.UUID, *, subscribed: bool
) -> None:
    """If ``public.users`` has a row for this id (API-key auth), mirror paid vs free tier."""
    u = session.get(User, user_id)
    if u is None:
        return
    u.tier = UserTier.PAID.value if subscribed else UserTier.FREE.value


def apply_word_charge(session: Session, profile: Profile, b: WordChargeBreakdown) -> None:
    """Persist deductions after a completed translation."""
    fc = int(profile.free_credits or 0)
    rb = int(profile.referral_bonus_words or 0)
    need_free_component = b.free_used

    take_fc = min(need_free_component, fc)
    need_free_component -= take_fc
    take_rb = min(need_free_component, rb)

    profile.free_credits = fc - take_fc
    profile.referral_bonus_words = rb - take_rb

    if bool(profile.subscription_active):
        profile.subscription_credits = max(
            0, int(profile.subscription_credits or 0) - b.subscription_used
        )

    if b.amount_to_pay > 0:
        bal = float(profile.credits_inr_balance or 0)
        profile.credits_inr_balance = bal - float(b.amount_to_pay)

    sync_plan_after_free_pool_depleted(profile)
    session.add(profile)


def activate_subscription_billing(
    profile: Profile,
    *,
    subscription_id: str | None = None,
    period_end: datetime | None = None,
    period_start: datetime | None = None,
    kind: str | None = None,
    now: datetime | None = None,
) -> None:
    """Activate or renew: reset subscription word pool; set plan + period timestamps."""
    now = now or datetime.now(timezone.utc)
    raw_kind = kind or profile.pending_subscription_kind
    if not raw_kind:
        p = str(profile.plan).lower()
        raw_kind = p if p in (PLAN_MONTHLY, PLAN_YEARLY) else PLAN_MONTHLY
    resolved_kind = str(raw_kind).strip().lower()
    if resolved_kind not in (PLAN_MONTHLY, PLAN_YEARLY):
        resolved_kind = PLAN_MONTHLY
    profile.pending_subscription_kind = None

    profile.subscription_active = True
    profile.subscription_credits = SUBSCRIPTION_CREDITS_MONTHLY

    ps = _ensure_aware_utc(period_start)
    pe = _ensure_aware_utc(period_end)

    if resolved_kind == PLAN_YEARLY:
        profile.plan = PLAN_YEARLY
        anchor = ps or now
        if profile.subscription_started_at is None:
            profile.subscription_started_at = anchor
        profile.subscription_period_start = anchor
        contract = now + timedelta(days=SUBSCRIPTION_YEAR_DAYS)
        if pe is not None:
            new_exp = _ensure_aware_utc(pe)
            old_c = _ensure_aware_utc(profile.subscription_contract_end)
            if new_exp is not None and old_c is not None:
                profile.subscription_contract_end = max(new_exp, old_c)
            elif new_exp is not None:
                profile.subscription_contract_end = new_exp
            else:
                profile.subscription_contract_end = contract
        else:
            old_c = _ensure_aware_utc(profile.subscription_contract_end)
            if old_c is not None and old_c > contract:
                profile.subscription_contract_end = old_c
            else:
                profile.subscription_contract_end = contract
        ce = _ensure_aware_utc(profile.subscription_contract_end)
        if ce is not None:
            ex_old = _ensure_aware_utc(profile.subscription_expiry)
            profile.subscription_expiry = max(ce, ex_old) if ex_old is not None else ce
    else:
        profile.plan = PLAN_MONTHLY
        profile.subscription_contract_end = None
        anchor = ps or now
        if ps is not None:
            profile.subscription_period_start = ps
        elif profile.subscription_period_start is None:
            profile.subscription_period_start = anchor
        if profile.subscription_started_at is None:
            profile.subscription_started_at = anchor
        if pe is not None:
            new_pe = _ensure_aware_utc(pe)
            ex_old = _ensure_aware_utc(profile.subscription_expiry)
            if ex_old is not None and new_pe is not None:
                profile.subscription_expiry = max(new_pe, ex_old)
            else:
                profile.subscription_expiry = new_pe
        else:
            profile.subscription_expiry = now + timedelta(days=SUBSCRIPTION_PERIOD_DAYS)

    if subscription_id:
        profile.razorpay_subscription_id = subscription_id


def deactivate_subscription_billing(profile: Profile) -> None:
    profile.subscription_active = False
    profile.subscription_credits = 0
    profile.subscription_contract_end = None
    profile.pending_subscription_kind = None
    _plan_after_subscription_ends(profile)


def add_usage_row(
    session: Session,
    *,
    user_id,
    job_id,
    word_units: int,
    payg_inr: float,
) -> None:
    # cost_inr is actual pay-as-you-go INR charged (0 when free/subscription cover all words).
    session.add(
        UsageRecord(
            user_id=user_id,
            job_id=job_id,
            tokens_used=max(0, int(word_units)),
            cost_inr=float(payg_inr or 0),
        )
    )
