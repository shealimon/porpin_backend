"""Referral link: claim after signup, stats for invite dashboard."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from app.db.models import Profile, ReferralAttribution
from app.db.session import get_session_factory
from app.deps.supabase_auth import AuthProfile, require_auth_profile
from app.limiter import limiter, user_or_ip_key
from app.services.referral_lifecycle import referral_ui_message
from app.services.referrals import claim_referral

router = APIRouter(prefix="/api/referrals", tags=["referrals"])


def _client_ip(request: Request) -> str | None:
    xff = request.headers.get("x-forwarded-for") or request.headers.get("X-Forwarded-For")
    if xff:
        return xff.split(",")[0].strip()[:45] or None
    if request.client:
        return (request.client.host or "")[:45] or None
    return None


def _mask_email(raw: str | None) -> str | None:
    if not raw or "@" not in raw:
        return None
    local, _, domain = raw.partition("@")
    if not local:
        return f"***@{domain}"
    return f"{local[0]}***@{domain}"


class ClaimReferralBody(BaseModel):
    code: str = Field(min_length=3, max_length=32)
    device_id: str | None = Field(default=None, max_length=256)


class ClaimReferralResponse(BaseModel):
    outcome: Literal[
        "credited",
        "already_attributed",
        "invalid_code",
        "self_referral",
        "email_blocked",
        "device_reused",
    ]
    words_credited_to_referrer: int = 0


class ReferralStatsResponse(BaseModel):
    """Referrer-facing counters; ``invites_total`` is friends who claimed your code."""

    invites_total: int
    pending_signup: int
    verified_pending_use: int
    rewarded_completed: int = Field(
        description="Completed invites where the referrer received at least one word credit.",
    )
    successful_conversions: int = Field(
        description="All completed invites (paid or reward-cap path), including zero-payout completions.",
    )
    rewarded_cap: int
    referrer_max_words_cap: int
    total_words_earned_from_referrals: int


class ReferralInviteRow(BaseModel):
    id: str
    status: str
    referee_email_masked: str | None
    ui_message: str
    created_at: str


class ReferralInvitesResponse(BaseModel):
    invites: list[ReferralInviteRow]


@router.post("/claim", response_model=ClaimReferralResponse)
@limiter.limit("20/minute", key_func=user_or_ip_key)
def claim_referral_endpoint(
    request: Request,
    body: ClaimReferralBody,
    profile: AuthProfile = Depends(require_auth_profile),
):
    factory = get_session_factory()
    if factory is None:
        raise HTTPException(status_code=503, detail="Database is not configured.")

    with factory() as session:
        outcome, credited = claim_referral(
            session,
            profile.id,
            body.code,
            claim_ip=_client_ip(request),
            device_id=body.device_id,
        )
        if outcome == "credited":
            session.commit()
        else:
            session.rollback()

    return ClaimReferralResponse(outcome=outcome, words_credited_to_referrer=credited)


@router.get("/stats", response_model=ReferralStatsResponse)
@limiter.limit("60/minute", key_func=user_or_ip_key)
def referral_stats(
    request: Request,
    profile: AuthProfile = Depends(require_auth_profile),
):
    _ = request
    from app.core.pipeline_settings import get_pipeline_settings  # noqa: PLC0415

    factory = get_session_factory()
    if factory is None:
        raise HTTPException(status_code=503, detail="Database is not configured.")
    settings = get_pipeline_settings()
    cap = max(0, int(settings.referral_max_rewarded_referrals))
    words_cap = max(0, int(settings.referral_max_words_earned_per_referrer))
    with factory() as session:
        rid = profile.id
        invites_total = int(
            session.scalar(
                select(func.count()).select_from(ReferralAttribution).where(
                    ReferralAttribution.referrer_user_id == rid
                )
            )
            or 0
        )
        pending_signup = int(
            session.scalar(
                select(func.count()).select_from(ReferralAttribution).where(
                    ReferralAttribution.referrer_user_id == rid,
                    ReferralAttribution.status == "pending",
                )
            )
            or 0
        )
        verified_pending_use = int(
            session.scalar(
                select(func.count()).select_from(ReferralAttribution).where(
                    ReferralAttribution.referrer_user_id == rid,
                    ReferralAttribution.status == "verified",
                )
            )
            or 0
        )
        rewarded_completed = int(
            session.scalar(
                select(func.count()).select_from(ReferralAttribution).where(
                    ReferralAttribution.referrer_user_id == rid,
                    ReferralAttribution.status == "completed",
                    ReferralAttribution.words_credited_to_referrer > 0,
                )
            )
            or 0
        )
        successful_conversions = int(
            session.scalar(
                select(func.count()).select_from(ReferralAttribution).where(
                    ReferralAttribution.referrer_user_id == rid,
                    ReferralAttribution.status == "completed",
                )
            )
            or 0
        )
        p = session.get(Profile, rid)
        total_words = int(p.referral_words_earned_total or 0) if p is not None else 0

    return ReferralStatsResponse(
        invites_total=invites_total,
        pending_signup=pending_signup,
        verified_pending_use=verified_pending_use,
        rewarded_completed=rewarded_completed,
        successful_conversions=successful_conversions,
        rewarded_cap=cap,
        referrer_max_words_cap=words_cap,
        total_words_earned_from_referrals=total_words,
    )


@router.get("/invites", response_model=ReferralInvitesResponse)
@limiter.limit("60/minute", key_func=user_or_ip_key)
def referral_invites(
    request: Request,
    profile: AuthProfile = Depends(require_auth_profile),
):
    _ = request
    factory = get_session_factory()
    if factory is None:
        raise HTTPException(status_code=503, detail="Database is not configured.")
    with factory() as session:
        rows = session.execute(
            select(ReferralAttribution, Profile.email)
            .join(Profile, Profile.id == ReferralAttribution.referee_user_id)
            .where(ReferralAttribution.referrer_user_id == profile.id)
            .order_by(ReferralAttribution.created_at.desc())
        ).all()

    invites: list[ReferralInviteRow] = []
    for attr, email in rows:
        payout = int(attr.words_credited_to_referrer or 0)
        invites.append(
            ReferralInviteRow(
                id=str(attr.id),
                status=attr.status,
                referee_email_masked=_mask_email(email),
                ui_message=referral_ui_message(status=attr.status, payout_words=payout),
                created_at=attr.created_at.isoformat() if attr.created_at else "",
            )
        )
    return ReferralInvitesResponse(invites=invites)
