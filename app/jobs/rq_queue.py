"""RQ job enqueue (Redis). Uses rq 1.x for broad platform support.

Queue-based background processing (Celery-equivalent pattern): API enqueues job IDs;
``python -m app.workers.rq_worker`` runs prepare/finalize; ``python -m app.workers.chunk_worker``
drains the Redis chunk list for parallel OpenAI calls. Scale by adding worker processes.
"""

from __future__ import annotations

import logging

from fastapi import HTTPException
from rq import Queue
from rq.job import Retry

from app.billing_constants import is_high_priority_plan
from app.core.pipeline_settings import get_pipeline_settings
from app.jobs.redis_sync import get_sync_redis
from app.db.models import DocumentJob, Profile
from app.db.session import get_session_factory

logger = logging.getLogger(__name__)

_RQ_RETRY_INTERVALS_S = (15, 45, 120, 300, 600, 900, 1200, 1800, 2700, 3600)


def _rq_job_retry() -> Retry | None:
    settings = get_pipeline_settings()
    n = int(settings.rq_job_retry_max)
    if n <= 0:
        return None
    intervals = list(_RQ_RETRY_INTERVALS_S[:n])
    if len(intervals) < n:
        pad = _RQ_RETRY_INTERVALS_S[-1]
        intervals.extend([pad] * (n - len(intervals)))
    return Retry(max=n, interval=intervals)


def _queues() -> tuple[Queue, Queue]:
    settings = get_pipeline_settings()
    if not settings.redis_url:
        raise RuntimeError("REDIS_URL not configured")
    conn = get_sync_redis()
    high = Queue(settings.rq_high_priority_queue_name, connection=conn)
    default = Queue(settings.rq_queue_name, connection=conn)
    return high, default


def queue_depth_total() -> int:
    """Combined pending jobs in high + default queues."""
    try:
        hi, df = _queues()
        return len(hi) + len(df)
    except Exception as e:
        logger.warning("queue depth: %s", e)
        return 0


def chunk_queue_depth() -> int:
    """Pending messages on the Redis chunk translation queue."""
    try:
        from app.jobs.chunk_queue import chunk_queue_length

        return chunk_queue_length()
    except Exception as e:
        logger.warning("chunk queue depth: %s", e)
        return 0


def ensure_queue_capacity() -> None:
    settings = get_pipeline_settings()
    if settings.rq_max_queue_depth > 0:
        d = queue_depth_total()
        if d >= settings.rq_max_queue_depth:
            raise HTTPException(
                status_code=503,
                detail="Server is busy processing many jobs; please try again shortly.",
            )
    if settings.chunk_queue_max_messages > 0:
        c = chunk_queue_depth()
        if c >= settings.chunk_queue_max_messages:
            raise HTTPException(
                status_code=503,
                detail=(
                    "Translation chunk queue is at capacity; please try again in a few minutes."
                ),
            )


def enqueue_document_job(job_id: str, *, plan: str = "free") -> None:
    settings = get_pipeline_settings()
    if not settings.redis_url:
        raise RuntimeError("REDIS_URL not configured")
    conn = get_sync_redis()
    qname = (
        settings.rq_high_priority_queue_name
        if is_high_priority_plan(plan)
        else settings.rq_queue_name
    )
    q = Queue(qname, connection=conn)
    if settings.use_sharded_chunk_queue:
        fn = "app.workers.rq_tasks.prepare_document_translation_job"
        timeout = 3600
    else:
        fn = "app.workers.rq_tasks.process_document_job"
        timeout = 7200
    retry = _rq_job_retry()
    q.enqueue(
        fn,
        job_id,
        timeout=timeout,
        failure_ttl=86400,
        result_ttl=0,
        retry=retry,
    )
    logger.info("Enqueued job %s %s on queue %s", job_id, fn, qname)


def enqueue_finalize_translation_job(job_id: str) -> None:
    """Run after all chunk batches complete (or none)."""
    import uuid

    settings = get_pipeline_settings()
    if not settings.redis_url:
        raise RuntimeError("REDIS_URL not configured")
    plan = "free"
    factory = get_session_factory()
    if factory is not None:
        try:
            jid = uuid.UUID(job_id)
            with factory() as session:
                job = session.get(DocumentJob, jid)
                if job is not None:
                    prof = session.get(Profile, job.user_id)
                    if prof is not None and is_high_priority_plan(str(prof.plan)):
                        plan = "paid"
        except Exception:
            logger.debug("enqueue finalize: could not resolve plan", exc_info=True)
    conn = get_sync_redis()
    qname = (
        settings.rq_high_priority_queue_name
        if is_high_priority_plan(plan)
        else settings.rq_queue_name
    )
    q = Queue(qname, connection=conn)
    retry = _rq_job_retry()
    q.enqueue(
        "app.workers.rq_tasks.finalize_document_translation_job",
        job_id,
        timeout=7200,
        failure_ttl=86400,
        result_ttl=0,
        retry=retry,
    )
    logger.info("Enqueued finalize for job %s on %s", job_id, qname)


def queue_stats() -> dict:
    """Lengths and worker-visible queue names (monitoring)."""
    settings = get_pipeline_settings()
    if not settings.redis_url:
        return {"configured": False}
    try:
        conn = get_sync_redis()
        hi = Queue(settings.rq_high_priority_queue_name, connection=conn)
        df = Queue(settings.rq_queue_name, connection=conn)
        return {
            "configured": True,
            "queues": {
                settings.rq_high_priority_queue_name: len(hi),
                settings.rq_queue_name: len(df),
            },
            "total_queued": len(hi) + len(df),
            "chunk_queue_depth": chunk_queue_depth(),
        }
    except Exception as e:
        return {"configured": True, "error": str(e)}
