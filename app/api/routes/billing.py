"""Razorpay subscription (₹999/mo) — create subscription + webhook renewal."""

from __future__ import annotations

import json
import logging
import os
import uuid

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from typing import Any, Literal

from app.core.pipeline_settings import BACKEND_ROOT_DIR, get_pipeline_settings
from app.db.models import DocumentJob, JobStatus, Profile
from app.db.session import get_session_factory
from app.deps.supabase_auth import require_auth_profile_flexible
from app.services.razorpay_client import get_razorpay_client
from app.services.razorpay_webhook import process_razorpay_webhook_dict
from app.services.razorpay_standard_checkout import (
    CreateOrderRequest as StandardCreateOrderRequest,
    create_checkout_order,
    verify_checkout_payment,
)
from app.services.payment_capture import apply_razorpay_captured_order
from app.services.referral_lifecycle import try_referrer_payout_after_referee_event
from app.services.razorpay_webhook import _resolve_period_bounds
from app.services.word_credits import (
    activate_subscription_billing,
    set_legacy_api_user_subscription_tier,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/billing", tags=["billing"])

_MIN_CAPTURE_ORDER_INR = 1.0


def _razorpay_keyvals_from_env_file() -> dict[str, str]:
    """Read ``backend/.env`` for Razorpay keys when Pydantic/os.environ miss (deploy/proxy cases).

    Uses the same root as ``pipeline_settings`` (``BACKEND_ROOT_DIR``), not ``Path(__file__).parents``.
    """
    p = BACKEND_ROOT_DIR / ".env"
    if not p.is_file():
        logger.warning("Razorpay: expected .env at %s but file is missing.", p)
        return {}
    try:
        from dotenv import dotenv_values

        raw = dotenv_values(p, encoding="utf-8-sig")
    except Exception as e:
        logger.warning("could not read %s: %s", p, e)
        return {}
    out: dict[str, str] = {}
    for k, v in raw.items():
        if not k or v is None:
            continue
        s = str(v).strip()
        if not s or k not in {
            "RAZORPAY_KEY_ID",
            "RAZORPAY_KEY_SECRET",
            "RAZORPAY_PLAN_ID",
            "RAZORPAY_YEARLY_PLAN_ID",
        }:
            continue
        out[k] = s
    return out


def _res_razorpay_key_id() -> str:
    s = (get_pipeline_settings().razorpay_key_id or "").strip()
    if s:
        return s
    s = (os.environ.get("RAZORPAY_KEY_ID") or "").strip()
    if s:
        return s
    return _razorpay_keyvals_from_env_file().get("RAZORPAY_KEY_ID", "")


def _res_razorpay_key_secret() -> str:
    s = (get_pipeline_settings().razorpay_key_secret or "").strip()
    if s:
        return s
    s = (os.environ.get("RAZORPAY_KEY_SECRET") or "").strip()
    if s:
        return s
    return _razorpay_keyvals_from_env_file().get("RAZORPAY_KEY_SECRET", "")


def _res_razorpay_plan_id(
    for_kind: Literal["monthly", "yearly"],
) -> str:
    key = "RAZORPAY_YEARLY_PLAN_ID" if for_kind == "yearly" else "RAZORPAY_PLAN_ID"
    settings = get_pipeline_settings()
    if for_kind == "yearly":
        s = (settings.razorpay_yearly_plan_id or "").strip()
    else:
        s = (settings.razorpay_plan_id or "").strip()
    if s:
        return s
    s = (os.environ.get(key) or "").strip()
    if s:
        return s
    s = _razorpay_keyvals_from_env_file().get(key, "")
    if s:
        return s
    logger.error(
        "Razorpay %s is empty after settings, os.environ, and reading %s (exists=%s). "
        "If the API runs on another host, set this env on that host; local .env is not used there.",
        key,
        BACKEND_ROOT_DIR / ".env",
        (BACKEND_ROOT_DIR / ".env").is_file(),
    )
    return ""


class CreateSubscriptionBody(BaseModel):
    """Prefer JSON body; avoids ``kind`` being dropped from query strings on some proxies."""

    kind: Literal["monthly", "yearly"] = "monthly"


class RazorpayVerifyCapturedPayment(BaseModel):
    razorpay_order_id: str
    razorpay_payment_id: str
    razorpay_signature: str


class RazorpayPaygTranslationOrderCreate(BaseModel):
    job_id: uuid.UUID


async def handle_razorpay_webhook(request: Request) -> dict:
    """Shared handler for POST /webhooks/razorpay and POST /api/billing/webhooks/razorpay."""
    settings = get_pipeline_settings()
    secret = (settings.razorpay_webhook_secret or "").strip()
    key_id = (settings.razorpay_key_id or "").strip()
    key_secret = (settings.razorpay_key_secret or "").strip()
    body = await request.body()
    sig = request.headers.get("X-Razorpay-Signature") or request.headers.get(
        "x-razorpay-signature"
    )

    if secret:
        try:
            import razorpay

            client = razorpay.Client(auth=(key_id, key_secret)) if key_id and key_secret else None
            if client is None:
                raise HTTPException(status_code=503, detail="Razorpay keys not configured.")
            client.utility.verify_webhook_signature(body.decode("utf-8"), sig, secret)
        except HTTPException:
            raise
        except Exception as e:
            logger.warning("razorpay webhook signature verify failed: %s", e)
            raise HTTPException(status_code=400, detail="Invalid webhook signature.") from e

    try:
        payload = json.loads(body.decode("utf-8"))
    except json.JSONDecodeError as e:
        raise HTTPException(status_code=400, detail="Invalid JSON body.") from e

    event_name = str(payload.get("event") or "")
    logger.info(
        "razorpay webhook received event=%s account_id=%s",
        event_name,
        str(payload.get("account_id") or ""),
    )
    return process_razorpay_webhook_dict(payload)


@router.post("/razorpay/standard-create-order")
def razorpay_standard_create_order(body: StandardCreateOrderRequest) -> dict[str, str | int]:
    """Same as ``POST /api/create-order`` but under ``/api/billing`` (use this if the top-level path 404s)."""
    return create_checkout_order(body)


@router.post("/razorpay/standard-verify-payment")
def razorpay_standard_verify_payment(body: dict[str, Any]) -> dict[str, bool]:
    """Same as ``POST /api/verify-payment`` but under ``/api/billing``."""
    return verify_checkout_payment(body)


@router.post("/razorpay/create-payg-translation-order")
def razorpay_create_payg_translation_order(
    body: RazorpayPaygTranslationOrderCreate,
    profile=Depends(require_auth_profile_flexible),
):
    """Razorpay order for a document already uploaded with ``awaiting_payment`` (see /translate?deferred_payment)."""
    client, key_id = get_razorpay_client()
    if client is None or not key_id:
        raise HTTPException(
            status_code=503,
            detail="Razorpay is not configured (set RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET).",
        )
    factory = get_session_factory()
    if factory is None:
        raise HTTPException(status_code=503, detail="Database is not configured.")
    with factory() as session:
        job = session.get(DocumentJob, body.job_id)
        if job is None or job.user_id != profile.id:
            raise HTTPException(status_code=404, detail="Job not found.")
        if str(job.status) not in (JobStatus.AWAITING_PAYMENT.value, "awaiting_payment"):
            raise HTTPException(
                status_code=400,
                detail="Job is not waiting for payment (or already processing).",
            )
        amount_inr = round(float(job.quoted_payg_inr or 0), 2)
    if amount_inr < _MIN_CAPTURE_ORDER_INR:
        raise HTTPException(
            status_code=400,
            detail="Job has no quoted PAYG amount; re-upload with payg_quote_inr.",
        )
    amount_paise = int(round(amount_inr * 100))
    receipt = f"pj{body.job_id.hex[:32]}"[:40]
    try:
        order = client.order.create(
            {
                "amount": amount_paise,
                "currency": "INR",
                "receipt": receipt,
                "notes": {
                    "profile_id": str(profile.id),
                    "kind": "payg_translation",
                    "job_id": str(body.job_id),
                },
            }
        )
    except Exception as e:
        logger.exception("razorpay order.create (payg job) failed")
        raise HTTPException(status_code=502, detail=f"Razorpay error: {e}") from e
    oid = order.get("id")
    if not oid:
        raise HTTPException(status_code=502, detail="Razorpay returned no order id.")
    logger.info(
        "razorpay payg-translation order created order_id=%s job_id=%s profile_id=%s amount_inr=%s",
        oid,
        body.job_id,
        profile.id,
        amount_inr,
    )
    return {
        "order_id": str(oid),
        "key_id": key_id,
        "amount_inr": amount_inr,
        "amount_paise": amount_paise,
        "currency": "INR",
        "job_id": str(body.job_id),
    }


@router.post("/razorpay/verify-captured-payment")
def razorpay_verify_captured_payment(
    body: RazorpayVerifyCapturedPayment,
    profile=Depends(require_auth_profile_flexible),
):
    """Verify Checkout signature, credit INR for a pay-as-you-go job, and activate the job (idempotent)."""
    client, key_id = get_razorpay_client()
    if client is None or not key_id:
        raise HTTPException(
            status_code=503,
            detail="Razorpay is not configured (set RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET).",
        )
    params = {
        "razorpay_order_id": body.razorpay_order_id,
        "razorpay_payment_id": body.razorpay_payment_id,
        "razorpay_signature": body.razorpay_signature,
    }
    try:
        client.utility.verify_payment_signature(params)
    except Exception as e:
        logger.warning("razorpay signature verify failed: %s", e)
        raise HTTPException(status_code=400, detail="Payment verification failed.") from e
    try:
        pay = client.payment.fetch(body.razorpay_payment_id)
        order = client.order.fetch(body.razorpay_order_id)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Razorpay fetch failed: {e}") from e
    if not isinstance(pay, dict) or str(pay.get("order_id") or "") != str(body.razorpay_order_id):
        raise HTTPException(status_code=400, detail="Payment does not match order.")
    status = str(pay.get("status") or "")
    if status != "captured":
        raise HTTPException(
            status_code=400,
            detail=f"Payment not captured yet (status: {status or 'unknown'}).",
        )
    onotes = order.get("notes") if isinstance(order.get("notes"), dict) else {}
    if str(onotes.get("kind") or "") != "payg_translation" or str(
        onotes.get("profile_id") or ""
    ) != str(profile.id):
        raise HTTPException(
            status_code=403,
            detail="Order is not a pay-as-you-go job payment for this account.",
        )
    try:
        amount_inr = int(pay["amount"]) / 100.0
    except (KeyError, TypeError, ValueError) as e:
        raise HTTPException(status_code=502, detail="Invalid payment amount.") from e
    res = apply_razorpay_captured_order(
        profile_id=profile.id,
        payment_id=body.razorpay_payment_id,
        order_id=body.razorpay_order_id,
        amount_inr=amount_inr,
        order_notes=onotes,
    )
    logger.info(
        "razorpay in-app verify profile_id=%s payment_id=%s kind=%s credited_new=%s inr=%s",
        profile.id,
        body.razorpay_payment_id,
        res.kind,
        res.credited_new,
        amount_inr,
    )
    return {
        "ok": True,
        "credited_inr": amount_inr,
        "already_applied": not res.credited_new,
        "kind": res.kind,
        "job_activated": res.job_activated,
        "job_id": str(res.job_id) if res.job_id else None,
    }


@router.post("/razorpay/sync-subscription-after-checkout")
def razorpay_sync_subscription_after_checkout(
    profile=Depends(require_auth_profile_flexible),
):
    """Persist subscription activation from Razorpay when webhooks are delayed or unreachable (e.g. local dev).

    Call from the Checkout ``handler`` after the customer completes payment.
    """
    factory = get_session_factory()
    if factory is None:
        raise HTTPException(status_code=503, detail="Database is not configured.")
    client, _ = get_razorpay_client()
    if client is None:
        raise HTTPException(
            status_code=503,
            detail="Razorpay is not configured (keys missing).",
        )
    with factory() as session:
        row = session.get(Profile, profile.id)
        if row is None:
            raise HTTPException(status_code=404, detail="Profile not found.")
        sub_id = (row.razorpay_subscription_id or "").strip()
        if not sub_id:
            raise HTTPException(
                status_code=400,
                detail="No subscription on file. Open checkout from this app first.",
            )
        try:
            sub = client.subscription.fetch(sub_id)
        except Exception as e:
            logger.exception(
                "razorpay subscription.fetch failed profile_id=%s sub_id=%s",
                profile.id,
                sub_id,
            )
            raise HTTPException(status_code=502, detail=f"Razorpay error: {e}") from e
        if not isinstance(sub, dict):
            raise HTTPException(status_code=502, detail="Invalid Razorpay subscription response.")
        status = str(sub.get("status") or "").lower()
        if status not in ("active", "authenticated"):
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Subscription is not active yet (status={status!r}). "
                    "Wait a few seconds and retry, or rely on the Razorpay webhook."
                ),
            )
        pend, pstart = _resolve_period_bounds(sub, sub_id, client)
        activate_subscription_billing(
            row,
            subscription_id=sub_id,
            period_end=pend,
            period_start=pstart,
        )
        set_legacy_api_user_subscription_tier(session, row.id, subscribed=True)
        session.commit()
        session.refresh(row)
        plan_slug = str(row.plan)
        sub_active = bool(row.subscription_active)
        uid = row.id
    try_referrer_payout_after_referee_event(uid)
    logger.info(
        "razorpay subscription synced after checkout profile_id=%s sub_id=%s plan=%s",
        uid,
        sub_id,
        plan_slug,
    )
    return {"ok": True, "plan": plan_slug, "subscription_active": sub_active}


@router.post("/razorpay/create-subscription")
def razorpay_create_subscription(
    profile=Depends(require_auth_profile_flexible),
    body: CreateSubscriptionBody = Body(
        default=CreateSubscriptionBody(),
        description="``kind=monthly`` uses RAZORPAY_PLAN_ID; ``kind=yearly`` uses RAZORPAY_YEARLY_PLAN_ID.",
    ),
):
    """Create a Razorpay subscription; client opens Checkout with ``subscription_id`` + ``key_id``."""
    kind = body.kind
    key_id = _res_razorpay_key_id()
    key_secret = _res_razorpay_key_secret()
    plan_id = _res_razorpay_plan_id(kind)
    missing: list[str] = []
    if not key_id:
        missing.append("RAZORPAY_KEY_ID")
    if not key_secret:
        missing.append("RAZORPAY_KEY_SECRET")
    if not plan_id:
        missing.append("RAZORPAY_YEARLY_PLAN_ID" if kind == "yearly" else "RAZORPAY_PLAN_ID")
    if missing:
        raise HTTPException(
            status_code=503,
            detail=(
                f"Razorpay is not fully configured: set {', '.join(missing)}. "
                "For subscriptions, create a plan in the Razorpay Dashboard (Subscriptions → Plans) and copy its plan_id (e.g. plan_xxx). "
                "Use the same key mode (test vs live) as RAZORPAY_KEY_ID. "
                "If these are already in backend/.env, restart the API (uvicorn); settings load once at startup."
            ),
        )

    try:
        import razorpay
    except ImportError as e:
        raise HTTPException(
            status_code=503,
            detail="Install the razorpay Python package on the server.",
        ) from e

    client = razorpay.Client(auth=(key_id, key_secret))
    # Razorpay allows a higher total_count for monthly plans than for yearly (annual) intervals (cap 100).
    total_count = 100 if kind == "yearly" else 120
    try:
        sub = client.subscription.create(
            {
                "plan_id": plan_id,
                "customer_notify": 1,
                "total_count": total_count,
                "quantity": 1,
            }
        )
    except Exception as e:
        logger.exception("razorpay subscription.create failed profile_id=%s kind=%s", profile.id, kind)
        raise HTTPException(status_code=502, detail=f"Razorpay error: {e}") from e

    sub_id = sub.get("id")
    if not sub_id:
        raise HTTPException(status_code=502, detail="Razorpay returned no subscription id.")

    factory = get_session_factory()
    if factory is None:
        raise HTTPException(status_code=503, detail="Database is not configured.")
    with factory() as session:
        row = session.get(Profile, profile.id)
        if row is None:
            raise HTTPException(status_code=404, detail="Profile not found.")
        row.razorpay_subscription_id = str(sub_id)
        row.pending_subscription_kind = kind
        session.commit()

    logger.info(
        "razorpay subscription created sub_id=%s profile_id=%s kind=%s plan_id=%s",
        sub_id,
        profile.id,
        kind,
        plan_id,
    )
    return {"subscription_id": str(sub_id), "key_id": key_id, "plan_id": plan_id, "kind": kind}


@router.post("/webhooks/razorpay")
async def razorpay_webhook(request: Request):
    return await handle_razorpay_webhook(request)
