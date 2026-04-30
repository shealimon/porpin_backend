"""Redis-backed translation progress for polling (partial / chunk-level updates)."""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from app.core.pipeline_settings import get_pipeline_settings
from app.jobs.redis_sync import get_sync_redis

logger = logging.getLogger(__name__)

_PROGRESS_TTL_SEC = 86400 * 2


def _key(job_id: str) -> str:
    return f"translator:job:{job_id}:progress"


def publish_translation_progress(
    job_id: str,
    *,
    progress_percent: int,
    current_stage: str,
    batches_done: int | None = None,
    batches_total: int | None = None,
    segments_translated: int | None = None,
    segments_total: int | None = None,
    translation_target: str | None = None,
    translation_target_label: str | None = None,
) -> None:
    settings = get_pipeline_settings()
    if not settings.redis_url:
        return
    try:
        r = get_sync_redis()
        k = _key(job_id)
        prev: dict[str, Any] = {}
        raw_prev = r.get(k)
        if raw_prev:
            try:
                prev = json.loads(raw_prev)
            except (json.JSONDecodeError, TypeError):
                prev = {}
        new_pct = max(0, min(100, int(progress_percent)))
        prev_pct = 0
        try:
            prev_pct = int(prev.get("progress_percent") or 0)
        except (TypeError, ValueError):
            prev_pct = 0
        # Monotonic bar: concurrent pulse vs batch callbacks (or RQ workers) must not
        # regress the stored percent. Allow explicit failure reset.
        if current_stage == "failed" and new_pct == 0:
            out_pct = 0
        else:
            out_pct = max(prev_pct, new_pct)
        # Stale low-% writes should not rewind the stage label (e.g. chunk_queued vs translating).
        if out_pct > new_pct:
            out_stage = str(prev.get("current_stage") or current_stage)
        else:
            out_stage = current_stage
        payload: dict[str, Any] = {
            **prev,
            "progress_percent": out_pct,
            "current_stage": out_stage,
            "updated_at": time.time(),
        }
        if batches_done is not None:
            payload["batches_done"] = int(batches_done)
        if batches_total is not None:
            payload["batches_total"] = int(batches_total)
        if segments_translated is not None:
            payload["segments_translated"] = int(segments_translated)
        if segments_total is not None:
            payload["segments_total"] = int(segments_total)
        if translation_target is not None:
            payload["translation_target"] = str(translation_target)
        if translation_target_label is not None:
            payload["translation_target_label"] = str(translation_target_label)
        r.set(k, json.dumps(payload), ex=_PROGRESS_TTL_SEC)
    except Exception:
        logger.debug("publish_translation_progress failed for job %s", job_id, exc_info=True)


def read_translation_progress(job_id: str) -> dict[str, Any] | None:
    settings = get_pipeline_settings()
    if not settings.redis_url:
        return None
    try:
        r = get_sync_redis()
        raw = r.get(_key(job_id))
        if not raw:
            return None
        return json.loads(raw)
    except Exception:
        logger.debug("read_translation_progress failed for job %s", job_id, exc_info=True)
        return None
