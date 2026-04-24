"""Shared SlowAPI rate limiter (IP-based + optional per-user key for jobs)."""

from __future__ import annotations

import hashlib

from fastapi import Request
from slowapi import Limiter
from slowapi.util import get_remote_address

limiter = Limiter(key_func=get_remote_address)


def user_or_ip_key(request: Request) -> str:
    """Rate limit by Supabase JWT / API key when present; else fall back to IP."""
    auth = request.headers.get("Authorization")
    if auth and auth.lower().startswith("bearer "):
        return "b:" + hashlib.sha256(auth.encode("utf-8")).hexdigest()[:40]
    ak = request.headers.get("X-API-Key")
    if ak:
        return "k:" + hashlib.sha256(ak.encode("utf-8")).hexdigest()[:40]
    return "ip:" + get_remote_address(request)
