"""Async translation jobs: Postgres + Redis RQ."""

from __future__ import annotations

import asyncio
import uuid

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from pydantic import BaseModel, Field

from app.api.schemas.export_format import ExportFormat
from app.db.models import DocumentJob, JobStatus
from app.db.session import get_session_factory
from app.jobs.rq_queue import queue_stats
from app.limiter import limiter, user_or_ip_key
from app.observability.metrics import worker_job_metrics
from app.deps.supabase_auth import AuthProfile, require_auth_profile_flexible
from app.services.async_job import create_and_enqueue_job_from_bytes

router = APIRouter(prefix="/jobs", tags=["jobs"])


class JobCreatedResponse(BaseModel):
    job_id: str = Field(description="Job id (UUID)")
    status: str


class JobGetResponse(BaseModel):
    status: str
    download_url: str | None = None
    error: str | None = None
    tokens_used: int | None = None
    cost_inr: float | None = None
    processing_time_seconds: float | None = None


class QueueStatsResponse(BaseModel):
    queues: dict[str, int] | None = None
    total_queued: int | None = None
    chunk_queue_depth: int | None = None
    configured: bool = False
    error: str | None = None


class MetricsSummaryResponse(BaseModel):
    jobs_processed: int
    jobs_failed: int
    success_rate: float
    avg_processing_seconds: float
    total_tokens_accounted: int


@router.post("", response_model=JobCreatedResponse)
@limiter.limit("180/minute", key_func=user_or_ip_key)
async def create_translation_job(
    request: Request,
    file: UploadFile = File(...),
    export: ExportFormat = ExportFormat.DOCX,
    profile: AuthProfile = Depends(require_auth_profile_flexible),
):
    data = await file.read()
    job_id = await asyncio.to_thread(
        create_and_enqueue_job_from_bytes,
        profile,
        file.filename or "upload",
        data,
        export,
    )
    return JobCreatedResponse(job_id=str(job_id), status=JobStatus.PENDING.value)


@router.get("/queue/stats", response_model=QueueStatsResponse)
@limiter.limit("300/minute", key_func=user_or_ip_key)
def get_queue_stats(
    request: Request,
    profile: AuthProfile = Depends(require_auth_profile_flexible),
):
    _ = profile
    stats = queue_stats()
    if not stats.get("configured"):
        return QueueStatsResponse(configured=False)
    if "error" in stats:
        return QueueStatsResponse(configured=True, error=stats["error"])
    return QueueStatsResponse(
        configured=True,
        queues=stats.get("queues"),
        total_queued=stats.get("total_queued"),
        chunk_queue_depth=stats.get("chunk_queue_depth"),
    )


@router.get("/metrics/summary", response_model=MetricsSummaryResponse)
@limiter.limit("300/minute", key_func=user_or_ip_key)
def get_metrics_summary(
    request: Request,
    profile: AuthProfile = Depends(require_auth_profile_flexible),
):
    _ = profile
    snap = worker_job_metrics.snapshot()
    return MetricsSummaryResponse(
        jobs_processed=snap["jobs_processed"],
        jobs_failed=snap["jobs_failed"],
        success_rate=snap["success_rate"],
        avg_processing_seconds=snap["avg_processing_seconds"],
        total_tokens_accounted=snap["total_tokens_accounted"],
    )


@router.get("/{job_id}", response_model=JobGetResponse)
@limiter.limit("600/minute", key_func=user_or_ip_key)
def get_translation_job(
    request: Request,
    job_id: str,
    profile: AuthProfile = Depends(require_auth_profile_flexible),
):
    factory = get_session_factory()
    if factory is None:
        raise HTTPException(status_code=503, detail="Database is not configured.")
    try:
        jid = uuid.UUID(job_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail="Invalid job id.") from e

    with factory() as session:
        job = session.get(DocumentJob, jid)
        if job is None:
            raise HTTPException(status_code=404, detail="Job not found.")
        if job.user_id != profile.id:
            raise HTTPException(status_code=403, detail="Not your job.")

        download_url = None
        if job.status == JobStatus.COMPLETED.value and job.output_file_path:
            download_url = f"/download/{job.id}"

        err = job.error_message if job.status == JobStatus.FAILED.value else None
        cost = float(job.cost_inr) if job.cost_inr is not None else None
        return JobGetResponse(
            status=job.status,
            download_url=download_url,
            error=err,
            tokens_used=job.tokens_used or None,
            cost_inr=cost,
            processing_time_seconds=job.processing_time_seconds,
        )
