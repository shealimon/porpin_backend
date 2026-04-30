"""Consumes Redis chunk translation messages (run several processes for horizontal scale).

Run: ``python -m app.workers.chunk_worker``
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import random
import sys
import time

import redis.asyncio as aioredis
from openai import AsyncOpenAI

from app.core.pipeline_settings import get_pipeline_settings
from app.jobs.chunk_finalize import schedule_finalize_if_terminal
from app.jobs.chunk_queue import (
    CHUNK_QUEUE_KEY,
    ChunkQueueMessage,
    acquire_global_inflight,
    get_job_chunk_counters,
    incr_job_chunks_done,
    incr_job_chunks_failed,
    pipeline_perf_incr_float,
    pipeline_perf_incr_int,
    release_chunk_lock,
    release_global_inflight,
    rpush_chunk_messages,
    set_chunk_status,
    try_acquire_chunk_lock,
    try_mark_batch_completed,
    try_mark_batch_failed,
)
from app.jobs.job_progress import publish_translation_progress
from app.services.translation_target import (
    normalize_translation_target,
    translation_target_label,
)
from app.services.translator.batch_translator import (
    build_async_openai_client,
    translate_one_chunk_batch_async,
)

logger = logging.getLogger(__name__)


async def _acquire_global_slot() -> None:
    s = get_pipeline_settings()
    while True:
        ok = await asyncio.to_thread(
            acquire_global_inflight,
            max_inflight=s.translate_global_max_inflight,
        )
        if ok:
            return
        await asyncio.sleep(s.chunk_inflight_spin_seconds)


async def _process_one_payload(payload: str, openai_client: AsyncOpenAI) -> None:
    settings = get_pipeline_settings()
    msg = ChunkQueueMessage.from_json(payload)
    job_id = msg.job_id
    bi = msg.batch_index
    dequeued_at = time.time()
    queue_wait_s = max(0.0, dequeued_at - msg.enqueued_at) if msg.enqueued_at else 0.0

    data_dir = settings.data_dir
    job_dir = data_dir / "jobs" / job_id
    work_dir = job_dir / "work"
    manifest_path = work_dir / "manifest.json"
    out_path = work_dir / "out" / f"batch_{bi}.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)

    if out_path.is_file():
        try:
            js = json.loads(out_path.read_text(encoding="utf-8"))
            if js.get("translations"):
                set_chunk_status(job_id, bi, "completed", note="idempotent_skip")
                if try_mark_batch_completed(job_id, bi):
                    await asyncio.to_thread(incr_job_chunks_done, job_id)
                schedule_finalize_if_terminal(job_id)
                return
        except Exception:
            logger.warning("corrupt batch output; re-translating job=%s batch=%s", job_id, bi)

    lock_acquired = False
    for _ in range(120):
        lock_acquired = await asyncio.to_thread(try_acquire_chunk_lock, job_id, bi)
        if lock_acquired:
            break
        await asyncio.sleep(0.5)
    if not lock_acquired:
        logger.warning("chunk lock busy; requeue job=%s batch=%s", job_id, bi)
        rpush_chunk_messages([msg])
        return

    set_chunk_status(job_id, bi, "processing", attempt=msg.attempt)
    try:
        if not manifest_path.is_file():
            logger.error("missing manifest job=%s", job_id)
            raise RuntimeError("manifest missing")
        t_manifest0 = time.perf_counter()
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        batches_raw = manifest["batches"]
        total_batches = max(1, len(batches_raw))
        segments = list(manifest["segments"])
        tt = normalize_translation_target(manifest.get("translation_target"))
        tt_label = translation_target_label(tt)
        indices = [int(x) for x in batches_raw[bi]]
        chunk_texts = [segments[i] for i in indices]
        manifest_read_s = time.perf_counter() - t_manifest0

        t_inflight0 = time.perf_counter()
        await _acquire_global_slot()
        global_inflight_wait_s = time.perf_counter() - t_inflight0
        try:
            usage_box: list[int] = [0]

            def bump(n: int) -> None:
                usage_box[0] += int(n or 0)

            def on_chunk_pulse(elapsed_s: float) -> None:
                _t, done_ct, _f = get_job_chunk_counters(job_id)
                t = total_batches
                floor = min(88, 23 + int(65 * done_ct / t))
                nxt = min(88, 23 + int(65 * (done_ct + 1) / t)) if done_ct < t else 88
                room = max(0, min(87, nxt) - floor)
                cand = floor + int(room * (1.0 - math.exp(-elapsed_s / 20.0)))
                cand = min(87, max(floor, cand))
                publish_translation_progress(
                    str(job_id),
                    progress_percent=cand,
                    current_stage="chunk_translating",
                    batches_done=done_ct,
                    batches_total=t,
                    translation_target=tt,
                    translation_target_label=tt_label,
                )

            translations, openai_pure_s = await translate_one_chunk_batch_async(
                chunk_texts,
                on_tokens=bump,
                openai_client=openai_client,
                on_translation_pulse=on_chunk_pulse,
                translation_target=tt,
            )
            if len(translations) != len(indices):
                raise RuntimeError("translation count mismatch")
            t_wr0 = time.perf_counter()
            out_path.write_text(
                json.dumps(
                    {
                        "indices": indices,
                        "translations": translations,
                        "usage_tokens": usage_box[0],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            result_write_s = time.perf_counter() - t_wr0

            pipeline_perf_incr_float(job_id, "chunk_queue_wait_sum_s", queue_wait_s)
            pipeline_perf_incr_float(job_id, "chunk_openai_api_sum_s", openai_pure_s)
            pipeline_perf_incr_float(
                job_id, "chunk_global_inflight_wait_sum_s", global_inflight_wait_s
            )
            pipeline_perf_incr_float(job_id, "chunk_manifest_read_sum_s", manifest_read_s)
            pipeline_perf_incr_float(job_id, "chunk_result_write_sum_s", result_write_s)
            pipeline_perf_incr_int(job_id, "chunk_batches_completed", 1)
            logger.info(
                "chunk_done job=%s batch=%s segments=%s queue_wait_s=%.3f "
                "global_inflight_wait_s=%.3f openai_api_s=%.3f manifest_read_s=%.3f "
                "result_write_s=%.3f tokens=%s",
                job_id,
                bi,
                len(indices),
                queue_wait_s,
                global_inflight_wait_s,
                openai_pure_s,
                manifest_read_s,
                result_write_s,
                usage_box[0],
            )
            if try_mark_batch_completed(job_id, bi):
                await asyncio.to_thread(incr_job_chunks_done, job_id)
            set_chunk_status(job_id, bi, "completed", usage_tokens=usage_box[0])
            schedule_finalize_if_terminal(job_id)
        finally:
            await asyncio.to_thread(release_global_inflight)
    except Exception:
        logger.exception("chunk_failed job=%s batch=%s attempt=%s", job_id, bi, msg.attempt)
        if msg.attempt < settings.chunk_task_max_retries:
            backoff = min(120.0, (2**msg.attempt) + random.random())
            await asyncio.sleep(backoff)
            rpush_chunk_messages(
                [
                    ChunkQueueMessage(
                        job_id=job_id,
                        batch_index=bi,
                        attempt=msg.attempt + 1,
                        enqueued_at=time.time(),
                    )
                ]
            )
        else:
            if await asyncio.to_thread(try_mark_batch_failed, job_id, bi):
                await asyncio.to_thread(incr_job_chunks_failed, job_id)
            set_chunk_status(job_id, bi, "failed", attempt=msg.attempt)
            schedule_finalize_if_terminal(job_id)
    finally:
        await asyncio.to_thread(release_chunk_lock, job_id, bi)


async def _popper(work_q: asyncio.Queue, redis_a: aioredis.Redis) -> None:
    while True:
        try:
            raw = await redis_a.blpop(CHUNK_QUEUE_KEY, timeout=5)
        except Exception:
            logger.exception("BLPOP failed")
            await asyncio.sleep(1)
            continue
        if raw is None:
            continue
        _, payload = raw
        if isinstance(payload, bytes):
            payload = payload.decode("utf-8")
        await work_q.put(str(payload))


async def _handler(work_q: asyncio.Queue, openai_client: AsyncOpenAI) -> None:
    while True:
        payload = await work_q.get()
        try:
            await _process_one_payload(payload, openai_client)
        finally:
            work_q.task_done()


async def _run_async() -> None:
    settings = get_pipeline_settings()
    if not settings.redis_url:
        raise SystemExit("REDIS_URL is not set.")
    if not settings.openai_api_key:
        logger.warning("OPENAI_API_KEY not set; chunk translation will fail.")

    redis_a = aioredis.from_url(
        settings.redis_url,
        decode_responses=True,
        max_connections=max(32, settings.redis_max_connections),
    )
    openai_client = build_async_openai_client()
    n = max(1, settings.chunk_worker_parallel_handlers)
    max_q = max(n * 4, 32)
    work_q: asyncio.Queue = asyncio.Queue(maxsize=max_q)
    pop_task = asyncio.create_task(_popper(work_q, redis_a))
    handlers = [
        asyncio.create_task(_handler(work_q, openai_client)) for _ in range(n)
    ]
    logger.info(
        "chunk_worker started handlers=%s queue=%s global_inflight_cap=%s",
        n,
        CHUNK_QUEUE_KEY,
        settings.translate_global_max_inflight,
    )
    try:
        await asyncio.gather(pop_task, *handlers)
    finally:
        try:
            await openai_client.close()
        except Exception:
            logger.exception("OpenAI client close failed")
        await redis_a.aclose()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        stream=sys.stdout,
    )
    try:
        asyncio.run(_run_async())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
