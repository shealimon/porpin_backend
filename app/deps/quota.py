"""Per-user daily job quotas (Redis) for free vs paid tiers."""

from __future__ import annotations

import uuid
from datetime import date

from fastapi import HTTPException

from app.billing_constants import is_high_priority_plan
from app.core.pipeline_settings import get_pipeline_settings
from app.db.models import User
from app.jobs.redis_sync import get_sync_redis


def _redis():
    if not get_pipeline_settings().redis_url:
        return None
    return get_sync_redis()


def _day_key(user: User) -> str:
    day = date.today().isoformat()
    return f"quota:{user.id}:{day}"


def _limit_for(user: User) -> int:
    s = get_pipeline_settings()
    return s.paid_tier_daily_jobs if user.tier == "paid" else s.free_tier_daily_jobs


def reserve_quota_slot(user: User) -> None:
    """Increment daily counter; roll back and raise 429 if over limit."""
    r = _redis()
    if r is None:
        return
    key = _day_key(user)
    n = r.incr(key)
    if n == 1:
        r.expire(key, 86_400)
    if n > _limit_for(user):
        r.decr(key)
        raise HTTPException(
            status_code=429,
            detail="Daily translation quota exceeded for your plan.",
        )


def release_quota_slot(user: User) -> None:
    r = _redis()
    if r is None:
        return
    key = _day_key(user)
    r.decr(key)


def _day_key_profile(profile_id: uuid.UUID) -> str:
    day = date.today().isoformat()
    return f"quota:profile:{profile_id}:{day}"


def reserve_quota_slot_for_profile(
    profile_id: uuid.UUID, plan: str = "free"
) -> None:
    """Daily job cap keyed by Supabase profile id."""
    r = _redis()
    if r is None:
        return
    settings = get_pipeline_settings()
    key = _day_key_profile(profile_id)
    n = r.incr(key)
    if n == 1:
        r.expire(key, 86_400)
    limit = (
        settings.paid_tier_daily_jobs
        if is_high_priority_plan(plan)
        else settings.free_tier_daily_jobs
    )
    if n > limit:
        r.decr(key)
        raise HTTPException(
            status_code=429,
            detail="Daily translation quota exceeded.",
        )


def release_quota_slot_for_profile(profile_id: uuid.UUID) -> None:
    r = _redis()
    if r is None:
        return
    r.decr(_day_key_profile(profile_id))
