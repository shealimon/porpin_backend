"""Redis list queue for independent translation chunk jobs + global inflight gate."""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Any

from redis import Redis

from app.jobs.redis_sync import get_sync_redis

logger = logging.getLogger(__name__)

CHUNK_QUEUE_KEY = "translator:chunk:queue"
GLOBAL_INFLIGHT_KEY = "translator:global:openai_inflight"


_ACQUIRE_INFLIGHT_LUA = """
local v = tonumber(redis.call('GET', KEYS[1]) or '0')
local cap = tonumber(ARGV[1])
if v >= cap then return 0 end
redis.call('INCR', KEYS[1])
return 1
"""

_RELEASE_INFLIGHT_LUA = """
local v = tonumber(redis.call('GET', KEYS[1]) or '0')
if v > 0 then redis.call('DECR', KEYS[1]) end
return 1
"""


@dataclass(frozen=True)
class ChunkQueueMessage:
    job_id: str
    batch_index: int
    attempt: int = 0
    enqueued_at: float = 0.0

    def to_json(self) -> str:
        return json.dumps(
            {
                "job_id": self.job_id,
                "batch_index": self.batch_index,
                "attempt": self.attempt,
                "enqueued_at": self.enqueued_at,
            },
            separators=(",", ":"),
        )

    @staticmethod
    def from_json(raw: str) -> ChunkQueueMessage:
        d = json.loads(raw)
        return ChunkQueueMessage(
            job_id=str(d["job_id"]),
            batch_index=int(d["batch_index"]),
            attempt=int(d.get("attempt") or 0),
            enqueued_at=float(d.get("enqueued_at") or 0.0),
        )


def _conn() -> Redis:
    return get_sync_redis()


def chunk_queue_length() -> int:
    try:
        return int(_conn().llen(CHUNK_QUEUE_KEY))
    except Exception as e:
        logger.warning("chunk_queue_length: %s", e)
        return 0


def acquire_global_inflight(*, max_inflight: int) -> bool:
    """Return True if slot acquired (caller must release)."""
    r = _conn()
    ok = int(r.eval(_ACQUIRE_INFLIGHT_LUA, 1, GLOBAL_INFLIGHT_KEY, str(max_inflight)))
    return ok == 1


def release_global_inflight() -> None:
    try:
        _conn().eval(_RELEASE_INFLIGHT_LUA, 1, GLOBAL_INFLIGHT_KEY)
    except Exception:
        logger.exception("release_global_inflight failed")


def rpush_chunk_messages(msgs: list[ChunkQueueMessage]) -> int:
    r = _conn()
    if not msgs:
        return 0
    pipe = r.pipeline()
    for m in msgs:
        pipe.rpush(CHUNK_QUEUE_KEY, m.to_json())
    pipe.execute()
    return len(msgs)


def chunk_lock_key(job_id: str, batch_index: int) -> str:
    return f"translator:job:{job_id}:chunk_lock:{batch_index}"


def try_acquire_chunk_lock(job_id: str, batch_index: int, *, ttl_sec: int = 1800) -> bool:
    r = _conn()
    key = chunk_lock_key(job_id, batch_index)
    return bool(r.set(key, str(time.time()), nx=True, ex=ttl_sec))


def release_chunk_lock(job_id: str, batch_index: int) -> None:
    try:
        _conn().delete(chunk_lock_key(job_id, batch_index))
    except Exception:
        pass


def batch_done_marker_key(job_id: str, batch_index: int) -> str:
    return f"translator:job:{job_id}:batch_done:{batch_index}"


def try_mark_batch_completed(job_id: str, batch_index: int) -> bool:
    """Idempotent: return True if this call first marked batch success marker."""
    r = _conn()
    ok = bool(r.set(batch_done_marker_key(job_id, batch_index), "1", nx=True, ex=86400 * 7))
    return ok


def try_mark_batch_failed(job_id: str, batch_index: int) -> bool:
    """Idempotent permanent failure marker for one batch."""
    r = _conn()
    key = f"translator:job:{job_id}:batch_failed:{batch_index}"
    return bool(r.set(key, "1", nx=True, ex=86400 * 7))


def chunks_total_key(job_id: str) -> str:
    return f"translator:job:{job_id}:chunks_total"


def chunks_done_key(job_id: str) -> str:
    return f"translator:job:{job_id}:chunks_done"


def chunks_failed_key(job_id: str) -> str:
    return f"translator:job:{job_id}:chunks_failed"


def finalize_enqueued_key(job_id: str) -> str:
    return f"translator:job:{job_id}:finalize_enqueued"


def try_acquire_finalize_running(job_id: str, *, ttl_sec: int = 7200) -> bool:
    r = _conn()
    return bool(
        r.set(f"translator:job:{job_id}:finalize_running", "1", nx=True, ex=ttl_sec)
    )


def release_finalize_running(job_id: str) -> None:
    try:
        _conn().delete(f"translator:job:{job_id}:finalize_running")
    except Exception:
        pass


def set_job_chunk_totals(job_id: str, total_batches: int) -> None:
    r = _conn()
    k_t, k_d, k_f = chunks_total_key(job_id), chunks_done_key(job_id), chunks_failed_key(job_id)
    pipe = r.pipeline()
    pipe.set(k_t, str(total_batches), ex=86400 * 7)
    pipe.set(k_d, "0", ex=86400 * 7)
    pipe.set(k_f, "0", ex=86400 * 7)
    pipe.execute()


def incr_job_chunks_done(job_id: str) -> int:
    r = _conn()
    return int(r.incr(chunks_done_key(job_id)))


def incr_job_chunks_failed(job_id: str) -> int:
    r = _conn()
    return int(r.incr(chunks_failed_key(job_id)))


def get_job_chunk_counters(job_id: str) -> tuple[int, int, int]:
    r = _conn()
    t = int(r.get(chunks_total_key(job_id)) or 0)
    d = int(r.get(chunks_done_key(job_id)) or 0)
    f = int(r.get(chunks_failed_key(job_id)) or 0)
    return t, d, f


def set_chunk_status(job_id: str, batch_index: int, status: str, **extra: Any) -> None:
    r = _conn()
    hk = f"translator:job:{job_id}:chunk_states"
    payload = {"status": status, "updated_at": time.time(), **extra}
    r.hset(hk, str(batch_index), json.dumps(payload, separators=(",", ":")))
    r.expire(hk, 86400 * 7)


def try_set_finalize_enqueued(job_id: str) -> bool:
    r = _conn()
    return bool(r.set(finalize_enqueued_key(job_id), "1", nx=True, ex=86400 * 7))


def pipeline_perf_hash_key(job_id: str) -> str:
    return f"translator:job:{job_id}:pipeline_perf"


def pipeline_perf_incr_float(job_id: str, field: str, delta: float) -> None:
    """Accumulate a float metric across chunk workers (e.g. OpenAI seconds sum)."""
    if delta == 0.0:
        return
    try:
        r = _conn()
        k = pipeline_perf_hash_key(job_id)
        r.hincrbyfloat(k, field, float(delta))
        r.expire(k, 86400 * 7)
    except Exception:
        logger.warning("pipeline_perf_incr_float failed job=%s field=%s", job_id, field)


def pipeline_perf_incr_int(job_id: str, field: str, delta: int = 1) -> None:
    try:
        r = _conn()
        k = pipeline_perf_hash_key(job_id)
        r.hincrby(k, field, int(delta))
        r.expire(k, 86400 * 7)
    except Exception:
        logger.warning("pipeline_perf_incr_int failed job=%s field=%s", job_id, field)


def pipeline_perf_hset_str(job_id: str, mapping: dict[str, str]) -> None:
    if not mapping:
        return
    try:
        r = _conn()
        k = pipeline_perf_hash_key(job_id)
        r.hset(k, mapping=mapping)
        r.expire(k, 86400 * 7)
    except Exception:
        logger.warning("pipeline_perf_hset_str failed job=%s", job_id)


def pipeline_perf_hgetall(job_id: str) -> dict[str, str]:
    try:
        raw = _conn().hgetall(pipeline_perf_hash_key(job_id))
        return dict(raw) if raw else {}
    except Exception as e:
        logger.warning("pipeline_perf_hgetall failed job=%s: %s", job_id, e)
        return {}
