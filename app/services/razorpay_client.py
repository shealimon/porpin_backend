"""Shared Razorpay SDK client factory for routes and webhooks."""

from __future__ import annotations

from typing import Any

from app.core.pipeline_settings import get_pipeline_settings


def get_razorpay_client() -> tuple[Any | None, str | None]:
    """Return ``(client, key_id)`` or ``(None, None)`` if misconfigured."""
    settings = get_pipeline_settings()
    key_id = (settings.razorpay_key_id or "").strip()
    key_secret = (settings.razorpay_key_secret or "").strip()
    if not key_id or not key_secret:
        return None, None
    try:
        import razorpay
    except ImportError:
        return None, None
    return razorpay.Client(auth=(key_id, key_secret)), key_id
