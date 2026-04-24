"""Razorpay webhook parsing, idempotency, and subscription lifecycle updates."""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select

from app.db.models import PaymentTransaction, Profile
from app.db.session import get_session_factory
from app.services.razorpay_client import get_razorpay_client
from app.services.referral_lifecycle import try_referrer_payout_after_referee_event
from app.services.payment_capture import apply_razorpay_captured_order
from app.services.word_credits import (
    activate_subscription_billing,
    deactivate_subscription_billing,
    set_legacy_api_user_subscription_tier,
)

logger = logging.getLogger(__name__)

WEBHOOK_TX_PROVIDER = "razorpay_webhook"


def _period_end(ent: dict[str, Any]) -> datetime | None:
    for key in ("current_end", "charge_at", "end_at"):
        raw = ent.get(key)
        if raw is None:
            continue
        try:
            ts = int(raw)
            return datetime.fromtimestamp(ts, tz=timezone.utc)
        except (TypeError, ValueError, OSError):
            continue
    return None


def _period_start(ent: dict[str, Any]) -> datetime | None:
    for key in ("current_start", "start_at", "created_at"):
        raw = ent.get(key)
        if raw is None:
            continue
        try:
            ts = int(raw)
            return datetime.fromtimestamp(ts, tz=timezone.utc)
        except (TypeError, ValueError, OSError):
            continue
    return None


def webhook_dedup_key(payload: dict[str, Any], event: str) -> str | None:
    """Stable idempotency key per Razorpay delivery (fallback when root ``id`` is absent)."""
    rid = payload.get("id")
    if rid:
        return str(rid)
    pl = payload.get("payload")
    if not isinstance(pl, dict):
        return None
    pay = pl.get("payment")
    if isinstance(pay, dict):
        ent = pay.get("entity")
        if isinstance(ent, dict) and ent.get("id"):
            return f"{event}:pay_{ent.get('id')}"
    inv = pl.get("invoice")
    if isinstance(inv, dict):
        ent = inv.get("entity")
        if isinstance(ent, dict) and ent.get("id"):
            return f"{event}:inv_{ent.get('id')}"
    sub = pl.get("subscription")
    if isinstance(sub, dict):
        ent = sub.get("entity")
        if isinstance(ent, dict) and ent.get("id"):
            pay_inner = pl.get("payment")
            pay_id = None
            if isinstance(pay_inner, dict):
                pe = pay_inner.get("entity")
                if isinstance(pe, dict):
                    pay_id = pe.get("id")
            if pay_id:
                return f"{event}:sub_{ent.get('id')}:pay_{pay_id}"
            # Renewals: one idempotency key per billing period when Razorpay omits root ``id``.
            ce = ent.get("current_end") or ent.get("charge_at")
            if ce is not None:
                return f"{event}:sub_{ent.get('id')}:ce_{ce}"
            return f"{event}:sub_{ent.get('id')}"
    return None


def _payload_section(
    payload: dict[str, Any], key: str
) -> dict[str, Any] | None:
    pl = payload.get("payload")
    if not isinstance(pl, dict):
        return None
    block = pl.get(key)
    if not isinstance(block, dict):
        return None
    ent = block.get("entity")
    return ent if isinstance(ent, dict) else None


def extract_subscription_id_and_entity(payload: dict[str, Any]) -> tuple[str | None, dict[str, Any] | None]:
    """Resolve Razorpay subscription id and an entity dict for period hints (subscription or invoice)."""
    ent = _payload_section(payload, "subscription")
    if ent and ent.get("id"):
        return str(ent.get("id")), ent

    inv_ent = _payload_section(payload, "invoice")
    if inv_ent:
        sid = inv_ent.get("subscription_id")
        if sid:
            return str(sid), inv_ent

    pl = payload.get("payload")
    if isinstance(pl, dict):
        alt = pl.get("subscription.entity")
        if isinstance(alt, dict) and alt.get("id"):
            return str(alt.get("id")), alt

    pay_ent = _payload_section(payload, "payment")
    if pay_ent:
        sid = pay_ent.get("subscription_id")
        if sid:
            return str(sid), pay_ent

    return None, None


def _resolve_period_bounds(
    entity_hint: dict[str, Any] | None,
    sub_id: str,
    client: Any | None,
) -> tuple[datetime | None, datetime | None]:
    pend = _period_end(entity_hint) if entity_hint else None
    pstart = _period_start(entity_hint) if entity_hint else None
    if pend is not None and pstart is not None:
        return pend, pstart
    if client is None:
        return pend, pstart
    try:
        sub = client.subscription.fetch(sub_id)
    except Exception as e:
        logger.warning(
            "razorpay: subscription fetch failed for period bounds sub_id=%s err=%s",
            sub_id,
            e,
        )
        return pend, pstart
    if isinstance(sub, dict):
        pend = pend or _period_end(sub)
        pstart = pstart or _period_start(sub)
    return pend, pstart


def _webhook_already_processed(session: Any, dedup_key: str) -> bool:
    row = session.scalar(
        select(PaymentTransaction)
        .where(
            PaymentTransaction.external_id == dedup_key,
            PaymentTransaction.provider == WEBHOOK_TX_PROVIDER,
        )
        .limit(1)
    )
    return row is not None


def _mark_webhook_processed(session: Any, *, user_id: uuid.UUID, dedup_key: str) -> None:
    session.add(
        PaymentTransaction(
            user_id=user_id,
            amount_inr=0.0,
            provider=WEBHOOK_TX_PROVIDER,
            external_id=dedup_key,
            status="applied",
        )
    )


def _log_payment_failure(payload: dict[str, Any], event: str) -> None:
    pay_ent = _payload_section(payload, "payment")
    inv_ent = _payload_section(payload, "invoice")
    pay_id = str(pay_ent.get("id") or "") if pay_ent else ""
    sub_id = str(pay_ent.get("subscription_id") or "") if pay_ent else ""
    if not sub_id and inv_ent:
        sub_id = str(inv_ent.get("subscription_id") or "")
    err = None
    code = None
    if pay_ent:
        err = pay_ent.get("error_description")
        code = pay_ent.get("error_code")
    logger.warning(
        "razorpay webhook failure event=%s payment_id=%s subscription_id=%s error_code=%s error_description=%s",
        event,
        pay_id or "none",
        sub_id or "none",
        code,
        err,
    )


def process_razorpay_webhook_dict(payload: dict[str, Any]) -> dict[str, Any]:
    """Apply Razorpay webhook side effects (PAYG capture, subscription activate/deactivate)."""
    event = str(payload.get("event") or "")
    factory = get_session_factory()
    client, _ = get_razorpay_client()

    if event == "payment.captured":
        pay_ent = _payload_section(payload, "payment")
        if isinstance(pay_ent, dict):
            payment_id = str(pay_ent.get("id") or "")
            order_id = str(pay_ent.get("order_id") or "")
            sub_from_pay = str(pay_ent.get("subscription_id") or "")
            notes: dict[str, Any] = {}
            if order_id and client:
                try:
                    order = client.order.fetch(order_id)
                    on = order.get("notes") if isinstance(order, dict) else None
                    if isinstance(on, dict):
                        notes = on
                except Exception:
                    logger.warning("razorpay: could not fetch order %s for webhook", order_id)
            raw_notes = pay_ent.get("notes")
            if isinstance(raw_notes, dict) and raw_notes:
                notes = {**notes, **raw_notes}

            kind_note = str(notes.get("kind") or "")
            if kind_note == "payg_translation" and payment_id and order_id:
                pid_raw = notes.get("profile_id")
                try:
                    prof_uuid = uuid.UUID(str(pid_raw))
                except (ValueError, TypeError):
                    logger.info(
                        "razorpay payg webhook: bad profile_id in notes payment_id=%s",
                        payment_id,
                    )
                    return {"ok": True}
                amount_paise = pay_ent.get("amount")
                try:
                    amount_inr = int(amount_paise) / 100.0
                except (TypeError, ValueError):
                    return {"ok": True}
                try:
                    apply_razorpay_captured_order(
                        profile_id=prof_uuid,
                        payment_id=payment_id,
                        order_id=order_id,
                        amount_inr=amount_inr,
                        order_notes=notes,
                    )
                except Exception:
                    logger.exception("razorpay order capture webhook failed payment_id=%s", payment_id)
                    raise
                logger.info(
                    "razorpay order capture payment_id=%s profile_id=%s kind=%s",
                    payment_id,
                    prof_uuid,
                    kind_note,
                )
                return {"ok": True}

            if sub_from_pay and payment_id and factory is not None:
                dkey = webhook_dedup_key(payload, event) or f"{event}:pay_{payment_id}"
                pend, pstart = _resolve_period_bounds(pay_ent, sub_from_pay, client)
                with factory() as session:
                    if _webhook_already_processed(session, dkey):
                        logger.info(
                            "razorpay duplicate webhook ignored event=%s key=%s",
                            event,
                            dkey,
                        )
                        return {"ok": True}
                    row = session.scalar(
                        select(Profile)
                        .where(Profile.razorpay_subscription_id == sub_from_pay)
                        .limit(1)
                    )
                    if row is None:
                        logger.info(
                            "razorpay payment.captured: no profile for subscription %s",
                            sub_from_pay,
                        )
                        return {"ok": True}
                    activate_subscription_billing(
                        row,
                        subscription_id=sub_from_pay,
                        period_end=pend,
                        period_start=pstart,
                    )
                    set_legacy_api_user_subscription_tier(session, row.id, subscribed=True)
                    _mark_webhook_processed(session, user_id=row.id, dedup_key=dkey)
                    session.commit()
                    rid = row.id
                try_referrer_payout_after_referee_event(rid)
                logger.info(
                    "razorpay subscription payment captured sub_id=%s profile_id=%s",
                    sub_from_pay,
                    rid,
                )
                return {"ok": True}

        return {"ok": True}

    if event in ("payment.failed", "invoice.payment_failed"):
        _log_payment_failure(payload, event)
        return {"ok": True}

    sub_id, entity_obj = extract_subscription_id_and_entity(payload)
    if factory is None or not sub_id:
        if event.startswith("subscription.") or event.startswith("invoice."):
            logger.info("razorpay webhook: no subscription id in payload event=%s", event)
        return {"ok": True}

    dkey = webhook_dedup_key(payload, event)

    if event in (
        "subscription.activated",
        "subscription.charged",
        "subscription.authenticated",
        "invoice.paid",
    ):
        pend, pstart = _resolve_period_bounds(entity_obj, sub_id, client)
        eff_key = dkey or (
            f"{event}:sub_{sub_id}:pend_{int(pend.timestamp())}" if pend is not None else None
        )
        if eff_key is None:
            logger.warning(
                "razorpay: missing idempotency key event=%s sub=%s duplicate may double-apply",
                event,
                sub_id,
            )
        with factory() as session:
            if eff_key and _webhook_already_processed(session, eff_key):
                logger.info(
                    "razorpay duplicate webhook ignored event=%s key=%s sub=%s",
                    event,
                    eff_key,
                    sub_id,
                )
                return {"ok": True}
            row = session.scalar(
                select(Profile)
                .where(Profile.razorpay_subscription_id == sub_id)
                .limit(1)
            )
            if row is None:
                logger.info(
                    "razorpay webhook: no profile for subscription %s event=%s",
                    sub_id,
                    event,
                )
                return {"ok": True}
            activate_subscription_billing(
                row,
                subscription_id=sub_id,
                period_end=pend,
                period_start=pstart,
            )
            set_legacy_api_user_subscription_tier(session, row.id, subscribed=True)
            if eff_key:
                _mark_webhook_processed(session, user_id=row.id, dedup_key=eff_key)
            session.commit()
            profile_id = row.id
        try_referrer_payout_after_referee_event(profile_id)
        logger.info(
            "razorpay subscription activated/renewed sub_id=%s profile_id=%s event=%s",
            sub_id,
            profile_id,
            event,
        )
        return {"ok": True}

    if event in ("subscription.cancelled", "subscription.completed", "subscription.halted"):
        with factory() as session:
            row = session.scalar(
                select(Profile)
                .where(Profile.razorpay_subscription_id == sub_id)
                .limit(1)
            )
            if row is not None:
                deactivate_subscription_billing(row)
                set_legacy_api_user_subscription_tier(session, row.id, subscribed=False)
                session.commit()
        logger.info("razorpay webhook: deactivated subscription %s event=%s", sub_id, event)
        return {"ok": True}

    return {"ok": True}
