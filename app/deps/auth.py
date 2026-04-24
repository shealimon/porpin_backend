"""API key authentication (hashed keys in DB)."""

from __future__ import annotations

import hashlib
import logging
import uuid

from fastapi import Header, HTTPException
from sqlalchemy import select

from app.core.pipeline_settings import get_pipeline_settings
from app.db.models import User
from app.db.session import get_session_factory

logger = logging.getLogger(__name__)


def hash_api_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def lookup_user_by_raw_key(raw_key: str) -> User | None:
    factory = get_session_factory()
    if factory is None:
        return None
    h = hash_api_key(raw_key)
    with factory() as session:
        return session.scalar(select(User).where(User.api_key_hash == h))


def optional_api_key_user(
    x_api_key: str | None = Header(None, alias="X-API-Key"),
) -> User | None:
    if not x_api_key:
        return None
    return lookup_user_by_raw_key(x_api_key)


def require_api_key_user(
    x_api_key: str | None = Header(None, alias="X-API-Key"),
) -> User:
    if not x_api_key:
        raise HTTPException(status_code=401, detail="X-API-Key header is required.")
    if get_session_factory() is None:
        raise HTTPException(status_code=503, detail="Database is not configured.")
    u = lookup_user_by_raw_key(x_api_key)
    if u is None:
        raise HTTPException(status_code=403, detail="Invalid API key.")
    return u


def ensure_seed_user() -> None:
    settings = get_pipeline_settings()
    if not settings.seed_api_key:
        return
    factory = get_session_factory()
    if factory is None:
        return
    h = hash_api_key(settings.seed_api_key)
    seed_uid: uuid.UUID | None = None
    raw_uid = (settings.seed_api_key_user_id or "").strip()
    if raw_uid:
        try:
            seed_uid = uuid.UUID(raw_uid)
        except ValueError:
            logger.warning("SEED_API_KEY_USER_ID is not a valid UUID; seed user gets a random id.")
    with factory() as session:
        existing = session.scalar(select(User).where(User.api_key_hash == h))
        if existing is not None and seed_uid is not None and existing.id != seed_uid:
            # Old random UUID cannot have a profiles row (FK auth.users). Replace so
            # X-API-Key resolves to a Supabase auth id and jobs save correctly.
            logger.warning(
                "Replacing SEED_API_KEY user id %s with SEED_API_KEY_USER_ID %s",
                existing.id,
                seed_uid,
            )
            session.delete(existing)
            session.commit()
            existing = None
        if existing is None:
            u = (
                User(id=seed_uid, api_key_hash=h, tier="free")
                if seed_uid is not None
                else User(api_key_hash=h, tier="free")
            )
            session.add(u)
            session.commit()
