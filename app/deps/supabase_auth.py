"""Supabase JWT validation and Profile resolution (API key fallback for dev)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from app.billing_constants import FREE_CREDITS_INITIAL

from fastapi import Depends, Header, HTTPException
import jwt
from jwt import PyJWKClient
from jwt.exceptions import PyJWKClientConnectionError, PyJWKClientError
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.pipeline_settings import get_pipeline_settings
from app.db.models import Profile, User, UserTier
from app.db.session import get_session_factory
from app.deps.auth import lookup_user_by_raw_key
from app.services.referral_lifecycle import sync_referral_lifecycle_for_profile_row
from app.services.referrals import ensure_referral_code


@dataclass(frozen=True)
class AuthProfile:
    """Detached user context (safe after DB session closes)."""

    id: uuid.UUID
    email: str | None
    plan: str
    credits_inr_balance: float
    free_credits: int
    subscription_active: bool
    subscription_credits: int
    subscription_expiry: datetime | None
    first_name: str | None = None
    last_name: str | None = None
    mobile: str | None = None
    city: str | None = None
    country: str | None = None
    referral_code: str | None = None
    referral_bonus_words: int = 0
    referral_words_earned_total: int = 0
    referred_by_user_id: uuid.UUID | None = None
    subscription_started_at: datetime | None = None
    subscription_period_start: datetime | None = None
    subscription_contract_end: datetime | None = None


# Small leeway for exp between Supabase, client, and API server clocks.
_JWT_LEEWAY_SEC = 60


def _auth_user_exists(session: Session, uid: uuid.UUID) -> bool:
    """True if ``auth.users`` has this id (Supabase). Fails soft if ``auth`` schema is absent."""
    try:
        row = session.execute(
            text("SELECT 1 FROM auth.users WHERE id = CAST(:id AS uuid) LIMIT 1"),
            {"id": str(uid)},
        ).first()
        return row is not None
    except SQLAlchemyError:
        return False


def _auth_profile_from_legacy_api_user(u: User) -> AuthProfile:
    """``public.users`` API-key identity without a ``profiles`` row (no matching ``auth.users``)."""
    return AuthProfile(
        id=u.id,
        email=u.email,
        plan=u.tier,
        credits_inr_balance=0.0,
        free_credits=int(FREE_CREDITS_INITIAL),
        subscription_active=False,
        subscription_credits=0,
        subscription_expiry=None,
        first_name=None,
        last_name=None,
        mobile=None,
        city=None,
        country=None,
        referral_code=None,
        referral_bonus_words=0,
        referral_words_earned_total=0,
        referred_by_user_id=None,
    )


def _synthetic_auth_profile(uid: uuid.UUID) -> AuthProfile:
    return AuthProfile(
        id=uid,
        email=None,
        plan=UserTier.FREE.value,
        credits_inr_balance=0.0,
        free_credits=int(FREE_CREDITS_INITIAL),
        subscription_active=False,
        subscription_credits=0,
        subscription_expiry=None,
        first_name=None,
        last_name=None,
        mobile=None,
        city=None,
        country=None,
        referral_code=None,
        referral_bonus_words=0,
        referral_words_earned_total=0,
        referred_by_user_id=None,
    )


def _meta_str(meta: dict, key: str) -> str | None:
    raw = meta.get(key)
    if not isinstance(raw, str):
        return None
    t = raw.strip()
    return t or None


def _decode_supabase_access_token(
    token: str,
) -> tuple[uuid.UUID, str | None, str | None, str | None, str | None, str | None, str | None]:
    """sub, email, names, mobile, city, country from token (incl. user_metadata)."""
    settings = get_pipeline_settings()
    try:
        header = jwt.get_unverified_header(token)
    except jwt.exceptions.DecodeError as e:
        raise HTTPException(status_code=401, detail="Invalid token.") from e

    alg = header.get("alg")
    if not alg or not isinstance(alg, str):
        raise HTTPException(status_code=401, detail="Invalid token header.")

    base_url = (settings.supabase_url or "").strip().rstrip("/")
    expected_issuer = f"{base_url}/auth/v1" if base_url else None

    if alg == "HS256":
        secret = (settings.supabase_jwt_secret or "").strip()
        if not secret:
            raise HTTPException(
                status_code=503,
                detail="SUPABASE_JWT_SECRET is not configured (required for HS256 access tokens).",
            )
        try:
            decode_kw: dict = {
                "algorithms": ["HS256"],
                "audience": "authenticated",
                "leeway": _JWT_LEEWAY_SEC,
            }
            if expected_issuer:
                decode_kw["issuer"] = expected_issuer
            payload = jwt.decode(token, secret, **decode_kw)
        except jwt.PyJWTError as e:
            raise HTTPException(status_code=401, detail="Invalid or expired token.") from e
    else:
        if not expected_issuer:
            raise HTTPException(
                status_code=503,
                detail=(
                    "SUPABASE_URL is not set; required to verify RS256/EC access tokens "
                    "(JWT signing keys). Set it to your project URL from Supabase → API, "
                    "same as the frontend VITE_SUPABASE_URL host."
                ),
            )
        jwks_url = f"{expected_issuer}/.well-known/jwks.json"
        try:
            jwks_client = PyJWKClient(jwks_url)
            signing_key = jwks_client.get_signing_key_from_jwt(token)
        except PyJWKClientConnectionError as e:
            raise HTTPException(
                status_code=503,
                detail="Could not fetch Supabase JWKS; check SUPABASE_URL and network.",
            ) from e
        except (PyJWKClientError, jwt.PyJWTError) as e:
            raise HTTPException(status_code=401, detail="Invalid or expired token.") from e
        try:
            payload = jwt.decode(
                token,
                signing_key.key,
                algorithms=[alg],
                audience="authenticated",
                issuer=expected_issuer,
                leeway=_JWT_LEEWAY_SEC,
            )
        except jwt.PyJWTError as e:
            raise HTTPException(status_code=401, detail="Invalid or expired token.") from e
    sub = payload.get("sub")
    if not sub:
        raise HTTPException(status_code=401, detail="Token missing sub.")
    raw_email = payload.get("email")
    email = raw_email if isinstance(raw_email, str) else None
    fn: str | None = None
    ln: str | None = None
    mobile: str | None = None
    city: str | None = None
    country: str | None = None
    meta = payload.get("user_metadata")
    if isinstance(meta, dict):
        raw_fn = meta.get("first_name")
        raw_ln = meta.get("last_name")
        fn = raw_fn.strip() if isinstance(raw_fn, str) and raw_fn.strip() else None
        ln = raw_ln.strip() if isinstance(raw_ln, str) and raw_ln.strip() else None
        mobile = _meta_str(meta, "mobile")
        city = _meta_str(meta, "city")
        country = _meta_str(meta, "country")
    try:
        return (
            uuid.UUID(str(sub)),
            email,
            fn,
            ln,
            mobile,
            city,
            country,
        )
    except ValueError as e:
        raise HTTPException(status_code=401, detail="Invalid user id in token.") from e


def _to_auth_profile(p: Profile) -> AuthProfile:
    bal = p.credits_inr_balance
    if hasattr(bal, "__float__"):
        bal = float(bal)
    return AuthProfile(
        id=p.id,
        email=p.email,
        plan=p.plan,
        credits_inr_balance=float(bal),
        free_credits=int(p.free_credits if p.free_credits is not None else FREE_CREDITS_INITIAL),
        subscription_active=bool(p.subscription_active),
        subscription_credits=int(p.subscription_credits or 0),
        subscription_expiry=p.subscription_expiry,
        first_name=p.first_name,
        last_name=p.last_name,
        mobile=p.mobile,
        city=p.city,
        country=p.country,
        referral_code=p.referral_code,
        referral_bonus_words=int(p.referral_bonus_words or 0),
        referral_words_earned_total=int(p.referral_words_earned_total or 0),
        referred_by_user_id=p.referred_by_user_id,
        subscription_started_at=p.subscription_started_at,
        subscription_period_start=p.subscription_period_start,
        subscription_contract_end=p.subscription_contract_end,
    )


def resolve_auth_profile(
    authorization: str | None,
    x_api_key: str | None,
) -> AuthProfile:
    """Prefer `Authorization: Bearer <jwt>`; else `X-API-Key` (dev / automation)."""
    factory = get_session_factory()

    if factory is None:
        if authorization and authorization.lower().startswith("bearer "):
            raw = authorization.split(" ", 1)[1].strip()
            uid, email, fn, ln, mob, city, country = _decode_supabase_access_token(raw)
            return AuthProfile(
                id=uid,
                email=email,
                plan=UserTier.FREE.value,
                credits_inr_balance=0.0,
                free_credits=int(FREE_CREDITS_INITIAL),
                subscription_active=False,
                subscription_credits=0,
                subscription_expiry=None,
                first_name=fn,
                last_name=ln,
                mobile=mob,
                city=city,
                country=country,
                referral_code=None,
                referral_bonus_words=0,
                referral_words_earned_total=0,
                referred_by_user_id=None,
            )
        raise HTTPException(status_code=503, detail="Database is not configured.")

    with factory() as session:
        user_id: uuid.UUID | None = None
        email: str | None = None
        first_name: str | None = None
        last_name: str | None = None
        jwt_mobile: str | None = None
        jwt_city: str | None = None
        jwt_country: str | None = None
        api_key_user: User | None = None

        if authorization and authorization.lower().startswith("bearer "):
            raw = authorization.split(" ", 1)[1].strip()
            (
                user_id,
                email,
                first_name,
                last_name,
                jwt_mobile,
                jwt_city,
                jwt_country,
            ) = _decode_supabase_access_token(raw)
        elif x_api_key:
            if not get_pipeline_settings().supabase_allow_api_key_fallback:
                raise HTTPException(
                    status_code=401,
                    detail="API key authentication is disabled; use Authorization Bearer.",
                )
            user = lookup_user_by_raw_key(x_api_key)
            if user is None:
                raise HTTPException(status_code=403, detail="Invalid API key.")
            api_key_user = user
            user_id = user.id
            email = user.email
        else:
            raise HTTPException(
                status_code=401,
                detail="Authorization Bearer token or X-API-Key required.",
            )

        p = session.get(Profile, user_id)
        if p is None:
            if api_key_user is not None and not _auth_user_exists(session, user_id):
                return _auth_profile_from_legacy_api_user(api_key_user)
            p = Profile(
                id=user_id,
                email=email,
                first_name=first_name,
                last_name=last_name,
                mobile=jwt_mobile,
                city=jwt_city,
                country=jwt_country,
            )
            session.add(p)
            session.commit()
            session.refresh(p)
        else:
            changed = False
            if email and p.email != email:
                p.email = email
                changed = True
            if first_name and p.first_name != first_name:
                p.first_name = first_name
                changed = True
            if last_name and p.last_name != last_name:
                p.last_name = last_name
                changed = True
            if jwt_mobile and p.mobile != jwt_mobile:
                p.mobile = jwt_mobile
                changed = True
            if jwt_city and p.city != jwt_city:
                p.city = jwt_city
                changed = True
            if jwt_country and p.country != jwt_country:
                p.country = jwt_country
                changed = True
            if changed:
                session.commit()
                session.refresh(p)
        if ensure_referral_code(session, p):
            session.commit()
            session.refresh(p)
        if sync_referral_lifecycle_for_profile_row(session, p):
            session.commit()
            session.refresh(p)
        return _to_auth_profile(p)


def resolve_auth_profile_with_anonymous_fallback(
    authorization: str | None,
    x_api_key: str | None,
) -> AuthProfile:
    """Same as resolve_auth_profile when credentials are present; optional placeholder user for dev."""
    settings = get_pipeline_settings()
    has_bearer = bool(
        authorization and authorization.lower().startswith("bearer ")
    )
    has_api_key = bool(x_api_key)
    if not has_bearer and not has_api_key:
        if settings.allow_anonymous_jobs and settings.anonymous_job_user_id:
            try:
                uid = uuid.UUID(settings.anonymous_job_user_id.strip())
            except ValueError as e:
                raise HTTPException(
                    status_code=503,
                    detail="anonymous_job_user_id must be a valid UUID.",
                ) from e
            factory = get_session_factory()
            if factory is None:
                raise HTTPException(status_code=503, detail="Database is not configured.")
            with factory() as session:
                p = session.get(Profile, uid)
                if p is None:
                    if not _auth_user_exists(session, uid):
                        return _synthetic_auth_profile(uid)
                    p = Profile(id=uid, email=None)
                    session.add(p)
                    session.commit()
                    session.refresh(p)
                if ensure_referral_code(session, p):
                    session.commit()
                    session.refresh(p)
                if sync_referral_lifecycle_for_profile_row(session, p):
                    session.commit()
                    session.refresh(p)
                return _to_auth_profile(p)
        raise HTTPException(
            status_code=401,
            detail="Authorization Bearer token or X-API-Key required.",
        )
    return resolve_auth_profile(authorization, x_api_key)


def get_auth_profile(
    authorization: str | None = Header(None, alias="Authorization"),
    x_api_key: str | None = Header(None, alias="X-API-Key"),
) -> AuthProfile:
    return resolve_auth_profile(authorization, x_api_key)


def require_auth_profile(profile: AuthProfile = Depends(get_auth_profile)) -> AuthProfile:
    return profile


def get_auth_profile_flexible(
    authorization: str | None = Header(None, alias="Authorization"),
    x_api_key: str | None = Header(None, alias="X-API-Key"),
) -> AuthProfile:
    return resolve_auth_profile_with_anonymous_fallback(authorization, x_api_key)


def require_auth_profile_flexible(
    profile: AuthProfile = Depends(get_auth_profile_flexible),
) -> AuthProfile:
    return profile
