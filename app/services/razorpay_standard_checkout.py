"""Shared handlers for Razorpay Standard Web Checkout (create order + verify signature)."""

from __future__ import annotations

import logging
import uuid
from typing import Any

from fastapi import HTTPException
from pydantic import BaseModel, Field, field_validator

from app.services.razorpay_client import get_razorpay_client

logger = logging.getLogger(__name__)


class CreateOrderRequest(BaseModel):
    amount: int = Field(
        ...,
        description="Amount in the smallest currency unit (paise for INR).",
    )
    currency: str = Field(default="INR", min_length=3, max_length=3)
    receipt: str | None = Field(
        default=None,
        max_length=40,
        description="Optional idempotency / audit id (max 40 chars).",
    )

    @field_validator("amount")
    @classmethod
    def _amount_min(cls, v: int) -> int:
        if v < 100:
            raise ValueError("amount must be at least 100 (paise).")
        return v

    @field_validator("currency")
    @classmethod
    def _currency_upper(cls, v: str) -> str:
        u = (v or "INR").upper()
        if u != "INR":
            raise ValueError("Only INR is supported for this integration.")
        return u


def _is_likely_auth_error(exc: Exception) -> bool:
    s = str(exc).lower()
    return "auth" in s and (
        "fail" in s or "invalid" in s or "denied" in s or "credential" in s or "key" in s
    )


def create_checkout_order(body: CreateOrderRequest) -> dict[str, str | int]:
    client, key_id = get_razorpay_client()
    if client is None:
        raise HTTPException(
            status_code=503,
            detail="Razorpay is not configured (set RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET).",
        )
    receipt = (body.receipt or "").strip() or f"r{uuid.uuid4().hex[:32]}"
    receipt = receipt[:40]
    try:
        from razorpay.errors import BadRequestError, GatewayError, ServerError

        order = client.order.create(
            {
                "amount": body.amount,
                "currency": body.currency,
                "receipt": receipt,
            }
        )
    except BadRequestError as e:
        logger.warning("razorpay order.create bad request: %s", e)
        if _is_likely_auth_error(e):
            raise HTTPException(
                status_code=401,
                detail="Razorpay authentication failed. Check RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET.",
            ) from e
        raise HTTPException(status_code=500, detail=f"Razorpay error: {e}") from e
    except (ServerError, GatewayError) as e:
        if _is_likely_auth_error(e):
            raise HTTPException(
                status_code=401,
                detail="Razorpay authentication failed. Check RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET.",
            ) from e
        logger.exception("razorpay order.create failed")
        raise HTTPException(status_code=500, detail=f"Razorpay error: {e}") from e
    except Exception as e:
        logger.exception("razorpay order.create failed")
        if _is_likely_auth_error(e):
            raise HTTPException(
                status_code=401,
                detail="Razorpay authentication failed. Check RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET.",
            ) from e
        raise HTTPException(status_code=500, detail=f"Razorpay error: {e}") from e

    order_id = order.get("id")
    if not order_id:
        raise HTTPException(status_code=500, detail="Razorpay returned no order id.")
    return {
        "order_id": str(order_id),
        "amount": int(order.get("amount", body.amount)),
        "currency": str(order.get("currency", body.currency)),
        # Public key; must match Checkout `key` — avoids mismatch when frontend env uses a different key than the API.
        "key_id": str(key_id).strip(),
    }


def verify_checkout_payment(body: dict[str, Any]) -> dict[str, bool]:
    required = ("razorpay_order_id", "razorpay_payment_id", "razorpay_signature")
    missing = [k for k in required if not (isinstance(body.get(k), str) and str(body[k]).strip())]
    if missing:
        raise HTTPException(
            status_code=400,
            detail=f"Missing or empty fields: {', '.join(missing)}",
        )
    razorpay_order_id = str(body["razorpay_order_id"]).strip()
    razorpay_payment_id = str(body["razorpay_payment_id"]).strip()
    razorpay_signature = str(body["razorpay_signature"]).strip()

    client, _ = get_razorpay_client()
    if client is None:
        raise HTTPException(
            status_code=503,
            detail="Razorpay is not configured (set RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET).",
        )
    try:
        from razorpay.errors import SignatureVerificationError

        client.utility.verify_payment_signature(
            {
                "razorpay_order_id": razorpay_order_id,
                "razorpay_payment_id": razorpay_payment_id,
                "razorpay_signature": razorpay_signature,
            }
        )
    except SignatureVerificationError as e:
        logger.warning("razorpay payment signature mismatch: %s", e)
        raise HTTPException(status_code=400, detail="Invalid payment signature.") from e
    except Exception as e:
        logger.exception("razorpay verify payment failed")
        raise HTTPException(status_code=500, detail=f"Verification error: {e}") from e
    return {"success": True}
