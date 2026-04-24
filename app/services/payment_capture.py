"""Razorpay order capture: idempotent path for client verify and webhooks (PAYG per job)."""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select

from app.billing_constants import is_high_priority_plan
from app.db.models import DocumentJob, JobStatus, PaymentTransaction, Profile
from app.db.session import get_session_factory
from app.deps.quota import reserve_quota_slot_for_profile
from app.jobs.rq_queue import enqueue_document_job
from app.services.profile_inr_credit import credit_inr_from_razorpay

logger = logging.getLogger(__name__)

TX_KIND_PAYG_JOB = "payg_translation"


@dataclass(frozen=True)
class CaptureResult:
    kind: str
    credited_new: bool
    job_activated: bool
    job_id: uuid.UUID | None
    message: str


def _bg_enqueue_job(job_id: str, plan: str) -> None:
    try:
        enqueue_document_job(job_id, plan=plan)
    except Exception:
        logger.exception("enqueue after payment failed job_id=%s", job_id)


def _enqueue_job_async(profile_plan: str, job_id: uuid.UUID) -> None:
    import threading

    t = threading.Thread(
        target=_bg_enqueue_job,
        args=(str(job_id), str(profile_plan)),
        daemon=True,
    )
    t.start()


def try_activate_payg_job_after_payment(
    *,
    profile_id: uuid.UUID,
    job_id: uuid.UUID,
) -> bool:
    """Idempotent: move job from awaiting_payment → pending and enqueue (RQ) or start milestone thread."""
    factory = get_session_factory()
    if factory is None:
        return False
    plan_key = "free"
    rel_path = ""
    with factory() as session:
        job = session.get(DocumentJob, job_id)
        if job is None or job.user_id != profile_id:
            logger.warning("payg job activation: job missing or wrong user job_id=%s", job_id)
            return False
        if str(job.status) not in (JobStatus.AWAITING_PAYMENT.value, "awaiting_payment"):
            return False
        prof = session.get(Profile, profile_id)
        pl = str(prof.plan) if prof is not None else "free"
        if prof is not None and is_high_priority_plan(pl):
            plan_key = "paid"
        rel_path = str(job.input_file_path or "")
        job.status = JobStatus.PENDING.value
        job.error_message = None
        job.translation_attempt = 0
        session.commit()
    reserve_quota_slot_for_profile(profile_id, pl)
    is_milestone = rel_path.startswith("milestone_")
    if is_milestone:
        from app.api.routes.legacy_compat import activate_milestone_after_payg_payment

        activate_milestone_after_payg_payment(str(job_id))
        return True
    _enqueue_job_async(plan_key, job_id)
    return True


def apply_razorpay_captured_order(
    *,
    profile_id: uuid.UUID,
    payment_id: str,
    order_id: str,
    amount_inr: float,
    order_notes: dict[str, Any] | None,
) -> CaptureResult:
    """
    Idempotent by Razorpay payment_id on ``transactions``. Never double-credit.
    Only ``payg_translation`` orders: credit INR then activate the linked job.
    """
    kind_raw = (order_notes or {}).get("kind")
    if str(kind_raw or "") != "payg_translation":
        return CaptureResult(
            kind="unsupported",
            credited_new=False,
            job_activated=False,
            job_id=None,
            message="only pay-as-you-go per-job orders are supported",
        )
    jraw = (order_notes or {}).get("job_id")
    try:
        job_uuid = uuid.UUID(str(jraw))
    except (TypeError, ValueError):
        return CaptureResult(
            kind=TX_KIND_PAYG_JOB,
            credited_new=False,
            job_activated=False,
            job_id=None,
            message="payg_translation order missing job_id in notes",
        )

    credited = credit_inr_from_razorpay(
        profile_id=profile_id,
        payment_id=payment_id,
        amount_inr=amount_inr,
        kind=TX_KIND_PAYG_JOB,
        razorpay_order_id=order_id,
        job_id=job_uuid,
    )
    act = try_activate_payg_job_after_payment(profile_id=profile_id, job_id=job_uuid)  # type: ignore[arg-type]
    if act:
        return CaptureResult(
            kind=TX_KIND_PAYG_JOB,
            credited_new=credited,
            job_activated=True,
            job_id=job_uuid,
            message="activated",
        )
    return CaptureResult(
        kind=TX_KIND_PAYG_JOB,
        credited_new=credited,
        job_activated=False,
        job_id=job_uuid,
        message="job_already_queued_or_not_awaiting_payment",
    )


def payment_id_already_captured(payment_id: str) -> bool:
    """True if this Razorpay payment was already applied (client verify or webhook)."""
    factory = get_session_factory()
    if factory is None:
        return False
    with factory() as session:
        row = session.scalar(
            select(PaymentTransaction)
            .where(
                PaymentTransaction.external_id == payment_id,
                PaymentTransaction.provider == "razorpay_wallet",
            )
            .limit(1)
        )
        return row is not None
