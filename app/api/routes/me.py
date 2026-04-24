"""Current user profile: sync and update (Postgres `public.profiles`)."""

from __future__ import annotations

from dataclasses import replace

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from app.db.models import Profile
from app.db.session import get_session_factory
from app.deps.supabase_auth import AuthProfile, require_auth_profile_flexible
from app.limiter import limiter, user_or_ip_key

router = APIRouter(prefix="/api/me", tags=["me"])


class SyncProfileResponse(BaseModel):
    id: str = Field(description="Profile / auth user UUID")
    email: str | None
    plan: str
    credits_inr_balance: float = 0
    free_credits: int = 0
    subscription_active: bool = False
    subscription_credits: int = 0
    subscription_expiry: str | None = None
    subscription_started_at: str | None = None
    subscription_period_start: str | None = None
    subscription_contract_end: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    mobile: str | None = None
    city: str | None = None
    country: str | None = None
    referral_code: str | None = None
    referral_bonus_words: int = 0
    referral_words_earned_total: int = 0
    referred_by_user_id: str | None = None


class UpdateProfileBody(BaseModel):
    first_name: str | None = Field(default=None, max_length=100)
    last_name: str | None = Field(default=None, max_length=100)
    mobile: str | None = Field(default=None, max_length=32)
    city: str | None = Field(default=None, max_length=120)
    country: str | None = Field(default=None, max_length=120)


def _dt_iso(dt) -> str | None:
    return dt.isoformat() if dt is not None else None


def _auth_profile_to_response(profile: AuthProfile) -> SyncProfileResponse:
    exp = profile.subscription_expiry
    exp_iso = exp.isoformat() if exp is not None else None
    return SyncProfileResponse(
        id=str(profile.id),
        email=profile.email,
        plan=profile.plan,
        credits_inr_balance=float(profile.credits_inr_balance or 0),
        free_credits=int(profile.free_credits),
        subscription_active=bool(profile.subscription_active),
        subscription_credits=int(profile.subscription_credits),
        subscription_expiry=exp_iso,
        subscription_started_at=_dt_iso(profile.subscription_started_at),
        subscription_period_start=_dt_iso(profile.subscription_period_start),
        subscription_contract_end=_dt_iso(profile.subscription_contract_end),
        first_name=profile.first_name,
        last_name=profile.last_name,
        mobile=profile.mobile,
        city=profile.city,
        country=profile.country,
        referral_code=profile.referral_code,
        referral_bonus_words=profile.referral_bonus_words,
        referral_words_earned_total=profile.referral_words_earned_total,
        referred_by_user_id=str(profile.referred_by_user_id)
        if profile.referred_by_user_id
        else None,
    )


def _profile_orm_to_response(p: Profile) -> SyncProfileResponse:
    exp = p.subscription_expiry
    exp_iso = exp.isoformat() if exp is not None else None
    bal = p.credits_inr_balance
    if hasattr(bal, "__float__"):
        bal = float(bal)
    return SyncProfileResponse(
        id=str(p.id),
        email=p.email,
        plan=p.plan,
        credits_inr_balance=float(bal or 0),
        free_credits=int(p.free_credits or 0),
        subscription_active=bool(p.subscription_active),
        subscription_credits=int(p.subscription_credits or 0),
        subscription_expiry=exp_iso,
        subscription_started_at=_dt_iso(getattr(p, "subscription_started_at", None)),
        subscription_period_start=_dt_iso(getattr(p, "subscription_period_start", None)),
        subscription_contract_end=_dt_iso(getattr(p, "subscription_contract_end", None)),
        first_name=p.first_name,
        last_name=p.last_name,
        mobile=p.mobile,
        city=p.city,
        country=p.country,
        referral_code=p.referral_code,
        referral_bonus_words=int(p.referral_bonus_words or 0),
        referral_words_earned_total=int(p.referral_words_earned_total or 0),
        referred_by_user_id=str(p.referred_by_user_id) if p.referred_by_user_id else None,
    )


def _normalize_opt_str(value: str | None, max_len: int) -> str | None:
    if value is None:
        return None
    t = value.strip()
    if not t:
        return None
    return t[:max_len]


@router.post("/sync-profile", response_model=SyncProfileResponse)
@limiter.limit("30/minute", key_func=user_or_ip_key)
def sync_profile(
    request: Request,
    profile: AuthProfile = Depends(require_auth_profile_flexible),
) -> SyncProfileResponse:
    """Upsert is done in dependency chain; call once after login so the DB row exists before uploads."""
    _ = request
    return _auth_profile_to_response(profile)


@router.patch("/profile", response_model=SyncProfileResponse)
@limiter.limit("30/minute", key_func=user_or_ip_key)
def update_profile(
    request: Request,
    body: UpdateProfileBody,
    profile: AuthProfile = Depends(require_auth_profile_flexible),
) -> SyncProfileResponse:
    _ = request
    factory = get_session_factory()
    patch = body.model_dump(exclude_unset=True)
    if factory is None:
        merged = profile
        if "first_name" in patch:
            merged = replace(merged, first_name=_normalize_opt_str(body.first_name, 100))
        if "last_name" in patch:
            merged = replace(merged, last_name=_normalize_opt_str(body.last_name, 100))
        if "mobile" in patch:
            merged = replace(merged, mobile=_normalize_opt_str(body.mobile, 32))
        if "city" in patch:
            merged = replace(merged, city=_normalize_opt_str(body.city, 120))
        if "country" in patch:
            merged = replace(merged, country=_normalize_opt_str(body.country, 120))
        return _auth_profile_to_response(merged)

    with factory() as session:
        row = session.get(Profile, profile.id)
        if row is None:
            raise HTTPException(status_code=404, detail="Profile not found.")
        if "first_name" in patch:
            row.first_name = _normalize_opt_str(body.first_name, 100)
        if "last_name" in patch:
            row.last_name = _normalize_opt_str(body.last_name, 100)
        if "mobile" in patch:
            row.mobile = _normalize_opt_str(body.mobile, 32)
        if "city" in patch:
            row.city = _normalize_opt_str(body.city, 120)
        if "country" in patch:
            row.country = _normalize_opt_str(body.country, 120)
        session.commit()
        session.refresh(row)
        return _profile_orm_to_response(row)
