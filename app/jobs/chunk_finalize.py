"""When all chunk batches reach a terminal state, enqueue the single finalize RQ job."""

from __future__ import annotations

import logging

from app.jobs.chunk_queue import get_job_chunk_counters, try_set_finalize_enqueued
from app.jobs.job_progress import publish_translation_progress
from app.jobs.rq_queue import enqueue_finalize_translation_job

logger = logging.getLogger(__name__)


def schedule_finalize_if_terminal(job_id: str) -> None:
    total, ok, fail = get_job_chunk_counters(job_id)
    if total <= 0:
        return
    if ok + fail < total:
        pct = min(88, 23 + max(0, int(65 * ok / max(1, total))))
        publish_translation_progress(
            job_id,
            progress_percent=pct,
            current_stage="chunk_translating",
            batches_done=ok,
            batches_total=total,
        )
        return
    publish_translation_progress(
        job_id,
        progress_percent=88,
        current_stage="stitching",
        batches_done=ok,
        batches_total=total,
    )
    if try_set_finalize_enqueued(job_id):
        try:
            enqueue_finalize_translation_job(job_id)
            logger.info(
                "finalize scheduled job=%s ok=%s fail=%s total=%s",
                job_id,
                ok,
                fail,
                total,
            )
        except Exception:
            logger.exception("enqueue finalize failed job=%s", job_id)
