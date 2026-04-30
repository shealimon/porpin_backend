"""Create a DocumentJob row, persist upload, enqueue RQ worker."""

from __future__ import annotations

import time
import uuid
from datetime import datetime, timezone

from fastapi import HTTPException, UploadFile
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.api.schemas.export_format import ExportFormat
from app.core.pipeline_settings import get_pipeline_settings
from app.db.models import DocumentJob, JobStatus
from app.db.session import get_session_factory
from app.deps.quota import release_quota_slot_for_profile, reserve_quota_slot_for_profile
from app.deps.supabase_auth import AuthProfile
from app.jobs.rq_queue import enqueue_document_job, ensure_queue_capacity
from app.services.translation_target import normalize_translation_target
from app.utils.file_validation import (
    assert_allowed_filename,
    sha256_bytes,
    validate_file_bytes,
)


def _rough_payg_access_or_402(profile: AuthProfile, settings) -> None:
    """When PAYG checkout is required: need subscription, free/referral words, or min INR on account."""
    if not settings.payg_checkout_required:
        return
    now = datetime.now(timezone.utc)
    exp = profile.subscription_expiry
    if exp and exp.tzinfo is None:
        exp = exp.replace(tzinfo=timezone.utc)
    sub_ok = (
        profile.subscription_active
        and int(profile.subscription_credits or 0) > 0
        and (exp is None or exp > now)
    )
    if sub_ok:
        return
    if int(profile.free_credits or 0) + int(profile.referral_bonus_words or 0) > 0:
        return
    min_inr = float(settings.minimum_charge_inr or 0)
    if float(profile.credits_inr_balance or 0) >= min_inr:
        return
    raise HTTPException(
        status_code=402,
        detail=(
            "Pay-as-you-go requires INR on account, an active subscription with word allowance, "
            "or free/referral words. Pay for the job, subscribe, or upload with deferred_payment=true "
            "and payg_quote_inr for per-document checkout."
        ),
    )


def create_and_enqueue_job(
    profile: AuthProfile,
    file: UploadFile,
    export: ExportFormat,
) -> uuid.UUID:
    """Sync path: reads ``UploadFile`` (e.g. RQ or sync routes). Prefer async read in HTTP handlers."""
    try:
        data = file.file.read()
    finally:
        file.file.close()
    return create_and_enqueue_job_from_bytes(
        profile, file.filename or "upload", data, export
    )


def create_and_enqueue_job_from_bytes(
    profile: AuthProfile,
    filename: str,
    data: bytes,
    export: ExportFormat,
    *,
    deferred_payment: bool = False,
    payg_quote_inr: float | None = None,
    translation_target: str = "hinglish",
) -> uuid.UUID:
    """Enqueue after body is already in memory (use ``await upload.read()`` in async routes)."""
    settings = get_pipeline_settings()
    if not settings.database_url or not settings.redis_url:
        raise HTTPException(
            status_code=503,
            detail="Async jobs require SUPABASE_DATABASE_URL (or DATABASE_URL) and REDIS_URL.",
        )
    factory = get_session_factory()
    if factory is None:
        raise HTTPException(status_code=503, detail="Database is not configured.")
    if not settings.openai_api_key:
        raise HTTPException(
            status_code=503,
            detail="OPENAI_API_KEY is not configured for workers.",
        )

    if deferred_payment:
        q = float(payg_quote_inr or 0)
        if q <= 0:
            raise HTTPException(
                status_code=400,
                detail="deferred_payment requires a positive payg_quote_inr (estimated pay-as-you-go INR).",
            )
    else:
        _rough_payg_access_or_402(profile, settings)

    suffix = assert_allowed_filename(filename)
    t_upload0 = time.perf_counter()
    validate_file_bytes(suffix, data, max_bytes=settings.max_upload_bytes)
    content_hash = sha256_bytes(data)
    tt = normalize_translation_target(translation_target)

    with factory() as session:
        dup = session.execute(
            select(DocumentJob)
            .where(
                DocumentJob.user_id == profile.id,
                DocumentJob.content_hash == content_hash,
                DocumentJob.status.in_(
                    (
                        JobStatus.PENDING.value,
                        JobStatus.AWAITING_PAYMENT.value,
                        JobStatus.PROCESSING.value,
                        JobStatus.COMPLETED.value,
                    )
                ),
            )
            .limit(1)
        ).scalar_one_or_none()
        if dup is not None:
            return dup.id

    ensure_queue_capacity()

    was_retry = False
    job_id: uuid.UUID
    dest_path: str | None = None

    with factory() as session:
        failed = session.execute(
            select(DocumentJob)
            .where(
                DocumentJob.user_id == profile.id,
                DocumentJob.content_hash == content_hash,
                DocumentJob.status == JobStatus.FAILED.value,
            )
            .limit(1)
        ).scalar_one_or_none()

        if failed is not None:
            job_id = failed.id
            was_retry = True
        else:
            job_id = uuid.uuid4()

        rel = f"jobs/{job_id}/input{suffix}"
        dest = settings.data_dir / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        dest_path = str(dest)
        upload_wall_s = time.perf_counter() - t_upload0

        if failed is not None:
            initial_status = JobStatus.PENDING.value
            quote_snap = float(failed.quoted_payg_inr or 0)
        else:
            initial_status = (
                JobStatus.AWAITING_PAYMENT.value if deferred_payment else JobStatus.PENDING.value
            )
            quote_snap = round(float(payg_quote_inr or 0), 2) if deferred_payment else 0.0

        if failed is not None:
            failed.status = initial_status
            failed.input_filename = filename or "upload"
            failed.input_file_path = rel.replace("\\", "/")
            failed.export_format = export.value
            failed.output_file_path = None
            failed.error_message = None
            failed.completed_at = None
            failed.tokens_used = 0
            failed.cost_inr = 0  # type: ignore[assignment]
            failed.processing_time_seconds = None
            failed.file_type = suffix.lstrip(".")
            failed.content_hash = content_hash
            failed.quoted_payg_inr = quote_snap  # type: ignore[assignment]
            failed.translation_attempt = 0
            failed.translation_target = tt
        else:
            session.add(
                DocumentJob(
                    id=job_id,
                    user_id=profile.id,
                    status=initial_status,
                    input_filename=filename or "upload",
                    input_file_path=rel.replace("\\", "/"),
                    export_format=export.value,
                    content_hash=content_hash,
                    file_type=suffix.lstrip("."),
                    quoted_payg_inr=quote_snap,
                    translation_attempt=0,
                    translation_target=tt,
                )
            )
        try:
            session.commit()
        except IntegrityError as e:
            session.rollback()
            raise HTTPException(
                status_code=409,
                detail=(
                    "Could not save job to the database. Ensure a row exists in public.profiles "
                    "for your user id (sign in once with Supabase Auth so the app can create it, "
                    "or match SEED_API_KEY to a real auth user). See supabase/schema/schema.sql."
                ),
            ) from e

    if deferred_payment:
        try:
            from app.jobs.chunk_queue import pipeline_perf_hset_str

            pipeline_perf_hset_str(
                str(job_id),
                {"upload_read_validate_write_s": f"{upload_wall_s:.6f}"},
            )
        except Exception:
            pass
        return job_id

    try:
        from app.jobs.chunk_queue import pipeline_perf_hset_str

        pipeline_perf_hset_str(
            str(job_id),
            {"upload_read_validate_write_s": f"{upload_wall_s:.6f}"},
        )
    except Exception:
        pass

    reserve_quota_slot_for_profile(profile.id, profile.plan)
    try:
        enqueue_document_job(str(job_id), plan=profile.plan)
    except Exception as e:
        release_quota_slot_for_profile(profile.id)
        msg = (
            "Could not enqueue job for processing (Redis unreachable or REDIS_URL misconfigured). "
            f"{e!s}"
        )
        with factory() as session:
            row = session.get(DocumentJob, job_id)
            if row:
                row.status = JobStatus.FAILED.value
                row.error_message = msg[:8000]
                session.commit()
        raise HTTPException(
            status_code=503,
            detail="Could not queue job for processing; check REDIS_URL and that Redis is running.",
        ) from e

    return job_id
