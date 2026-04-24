"""In-process + optional Redis counters (workers report to Redis; API reads)."""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

_REDIS_KEYS = {
    "ok": "translator:metrics:jobs_ok",
    "fail": "translator:metrics:jobs_fail",
    "latency": "translator:metrics:latency_s",
    "tokens": "translator:metrics:tokens_total",
}


def _redis():
    try:
        from app.core.pipeline_settings import get_pipeline_settings
        from app.jobs.redis_sync import get_sync_redis

        if not get_pipeline_settings().redis_url:
            return None
        return get_sync_redis()
    except Exception as e:
        logger.debug("metrics redis: %s", e)
        return None


@dataclass
class JobMetrics:
    jobs_processed: int = 0
    jobs_failed: int = 0
    total_processing_seconds: float = 0.0
    total_tokens: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def record_success(self, processing_seconds: float, tokens: int) -> None:
        with self._lock:
            self.jobs_processed += 1
            self.total_processing_seconds += processing_seconds
            self.total_tokens += max(0, tokens)
        r = _redis()
        if r:
            try:
                r.incr(_REDIS_KEYS["ok"])
                r.incrby(_REDIS_KEYS["tokens"], max(0, tokens))
                r.incrbyfloat(_REDIS_KEYS["latency"], float(processing_seconds))
            except Exception as e:
                logger.debug("metrics redis write: %s", e)

    def record_failure(self) -> None:
        with self._lock:
            self.jobs_failed += 1
        r = _redis()
        if r:
            try:
                r.incr(_REDIS_KEYS["fail"])
            except Exception as e:
                logger.debug("metrics redis write: %s", e)

    def snapshot(self) -> dict:
        r = _redis()
        if r:
            try:
                ok = int(r.get(_REDIS_KEYS["ok"]) or 0)
                fail = int(r.get(_REDIS_KEYS["fail"]) or 0)
                lat = float(r.get(_REDIS_KEYS["latency"]) or 0)
                tok = int(r.get(_REDIS_KEYS["tokens"]) or 0)
                attempts = ok + fail
                success_rate = (ok / attempts) if attempts else 1.0
                avg_latency = (lat / ok) if ok else 0.0
                return {
                    "jobs_processed": ok,
                    "jobs_failed": fail,
                    "success_rate": round(success_rate, 4),
                    "avg_processing_seconds": round(avg_latency, 3),
                    "total_tokens_accounted": tok,
                    "wall_time": time.time(),
                }
            except Exception as e:
                logger.debug("metrics redis read: %s", e)

        with self._lock:
            proc = self.jobs_processed
            fail = self.jobs_failed
            tot = self.total_processing_seconds
            tok = self.total_tokens
        attempts = proc + fail
        success_rate = (proc / attempts) if attempts else 1.0
        avg_latency = (tot / proc) if proc else 0.0
        return {
            "jobs_processed": proc,
            "jobs_failed": fail,
            "success_rate": round(success_rate, 4),
            "avg_processing_seconds": round(avg_latency, 3),
            "total_tokens_accounted": tok,
            "wall_time": time.time(),
        }


worker_job_metrics = JobMetrics()
