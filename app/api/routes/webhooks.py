"""Stripe and other billing webhooks (stub for paid-tier activation)."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Header, HTTPException, Request

from app.api.routes.billing import handle_razorpay_webhook
from app.core.pipeline_settings import get_pipeline_settings

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post("/webhooks/stripe")
async def stripe_webhook(
    request: Request,
    stripe_signature: str | None = Header(None, alias="Stripe-Signature"),
):
    """
    Receives Stripe events. Verify ``Stripe-Signature`` with ``STRIPE_WEBHOOK_SECRET``
    in production; update user tier in Postgres on ``customer.subscription.*`` events.
    """
    settings = get_pipeline_settings()
    body = await request.body()
    if settings.stripe_webhook_secret and not stripe_signature:
        raise HTTPException(status_code=400, detail="Missing Stripe-Signature")
    logger.info(
        "stripe_webhook bytes=%s signature=%s",
        len(body),
        "present" if stripe_signature else "none",
    )
    return {"received": True}


@router.post("/webhooks/razorpay")
async def razorpay_webhook(request: Request):
    """Razorpay billing events (subscription charged / cancelled)."""
    return await handle_razorpay_webhook(request)
