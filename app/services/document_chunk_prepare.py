"""Split a document into Redis chunk-queue jobs (parse/classify/plan once)."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path

from app.core.pipeline_settings import get_pipeline_settings
from app.jobs.chunk_finalize import schedule_finalize_if_terminal
from app.jobs.chunk_queue import (
    ChunkQueueMessage,
    acquire_global_inflight,
    get_job_chunk_counters,
    incr_job_chunks_done,
    release_global_inflight,
    rpush_chunk_messages,
    set_chunk_status,
    set_job_chunk_totals,
    try_mark_batch_completed,
)
from app.models.document_models import ContentBlock
from app.services.parser import parse_document
from app.services.translation_plan import build_translation_plan
from app.services.translation_plan_serde import dump_manifest_v2, manifest_json_bytes
from app.services.translation_target import normalize_translation_target
from app.services.classifier.section_classifier import classify_blocks
from app.utils.token_batching import pack_segment_indices_by_tokens
from app.utils.word_batching import pack_segment_indices_by_words

logger = logging.getLogger(__name__)


def _log_chunk_prepare_perf(
    job_id: str,
    *,
    mode: str,
    num_batches: int,
    num_segments: int,
    breakdown: dict[str, float],
) -> None:
    """One-line stage totals for tuning (parse / plan / translate vs enqueue)."""
    parse_s = float(breakdown.get("parse_second_document_total_s", 0.0) or 0.0)
    parse_reused = float(breakdown.get("parse_second_skipped_reused_blocks", 0.0) or 0.0) >= 1.0
    cls_s = float(breakdown.get("classify_s", 0.0) or 0.0)
    plan_s = float(breakdown.get("translation_plan_and_chunking_s", 0.0) or 0.0)
    pack_s = float(breakdown.get("word_pack_queue_batches_s", 0.0) or 0.0)
    inl = float(breakdown.get("inline_translate_sequential_s", 0.0) or 0.0)
    enq = float(breakdown.get("redis_chunk_enqueue_s", 0.0) or 0.0)
    total = float(breakdown.get("prepare_chunk_jobs_total_s", 0.0) or 0.0)
    logger.info(
        "chunk_prepare_perf job=%s mode=%s batches=%s segments=%s "
        "parse_s=%.2f parse_reused_blocks=%s classify_s=%.2f plan_s=%.2f pack_s=%.2f "
        "inline_translate_s=%.2f redis_enqueue_s=%.2f total_prepare_s=%.2f",
        job_id,
        mode,
        num_batches,
        num_segments,
        parse_s,
        parse_reused,
        cls_s,
        plan_s,
        pack_s,
        inl,
        enq,
        total,
    )


def _inline_translate_batches(
    job_id: str,
    job_dir: Path,
    segments: list[str],
    batches: list[list[int]],
    *,
    translation_target: str,
) -> float:
    """Translate batches in-process with asyncio concurrency (same global inflight as chunk_worker).

    Previously this loop was strictly sequential, so multi-batch short jobs paid full
    round-trip latency per batch. Large books still use the Redis queue + many workers.

    Returns wall seconds for the inline translation phase.
    """
    return asyncio.run(
        _inline_translate_batches_async(
            job_id, job_dir, segments, batches, translation_target=translation_target
        )
    )


async def _inline_translate_batches_async(
    job_id: str,
    job_dir: Path,
    segments: list[str],
    batches: list[list[int]],
    *,
    translation_target: str,
) -> float:
    from app.services.translator.batch_translator import translate_one_chunk_batch_async

    settings = get_pipeline_settings()
    out_dir = job_dir / "work" / "out"
    out_dir.mkdir(parents=True, exist_ok=True)
    local_sem = asyncio.Semaphore(
        max(1, min(len(batches), settings.translate_batch_max_concurrency))
    )

    async def one_batch(bi: int, idxs: list[int]) -> tuple[int, list[int], list[str], int]:
        texts = [segments[i] for i in idxs]
        usage_box = [0]

        def _bump(n: int) -> None:
            usage_box[0] += int(n or 0)

        async with local_sem:
            while True:
                ok = await asyncio.to_thread(
                    acquire_global_inflight,
                    max_inflight=settings.translate_global_max_inflight,
                )
                if ok:
                    break
                await asyncio.sleep(settings.chunk_inflight_spin_seconds)
            try:
                translations, _dt = await translate_one_chunk_batch_async(
                    texts,
                    on_tokens=_bump,
                    translation_target=translation_target,
                )
            finally:
                await asyncio.to_thread(release_global_inflight)

        if len(translations) != len(idxs):
            raise RuntimeError(f"inline batch {bi}: translation length mismatch")
        return bi, list(idxs), list(translations), usage_box[0]

    t_inline = time.perf_counter()
    results = await asyncio.gather(
        *[one_batch(bi, idxs) for bi, idxs in enumerate(batches)]
    )
    for bi, idxs, translations, usage_tokens in sorted(results, key=lambda x: x[0]):
        out_path = out_dir / f"batch_{bi}.json"
        out_path.write_text(
            json.dumps(
                {
                    "indices": idxs,
                    "translations": translations,
                    "usage_tokens": usage_tokens,
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        set_chunk_status(job_id, bi, "completed", usage_tokens=usage_tokens)
        if try_mark_batch_completed(job_id, bi):
            incr_job_chunks_done(job_id)
    wall = time.perf_counter() - t_inline
    logger.info(
        "chunk_prepare stage=inline_translate batches=%s max_parallel=%s wall_s=%.3f job=%s",
        len(batches),
        min(len(batches), settings.translate_batch_max_concurrency),
        wall,
        job_id,
    )
    schedule_finalize_if_terminal(job_id)
    return wall


def prepare_document_chunk_jobs(
    *,
    job_id: str,
    input_path: Path,
    job_dir: Path,
    blocks: list[ContentBlock] | None = None,
    document_template_id: str | None = None,
    translation_target: str = "hinglish",
) -> tuple[int, float, dict[str, float]]:
    """
    Build ``work/manifest.json``, push chunk messages to Redis, init counters.
    Returns ``(num_batches, wall_seconds, stage_timings_s)``.

    Pass ``blocks`` when the caller already parsed ``input_path`` (e.g. RQ prepare)
    to avoid parsing the document twice — a major win for PDFs and large files.
    """
    t0 = time.perf_counter()
    breakdown: dict[str, float] = {}
    work_dir = job_dir / "work"
    work_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = work_dir / "manifest.json"

    # Repair: manifest on disk but Redis counters missing
    if manifest_path.is_file():
        jt, _, _ = get_job_chunk_counters(job_id)
        if jt > 0:
            logger.info("prepare_document_chunk_jobs idempotent skip job=%s", job_id)
            return jt, time.perf_counter() - t0, breakdown
        try:
            raw = json.loads(manifest_path.read_text(encoding="utf-8"))
            batches = [list(map(int, b)) for b in raw["batches"]]
            set_job_chunk_totals(job_id, len(batches))
            for i in range(len(batches)):
                set_chunk_status(job_id, i, "pending")
            msgs = [
                ChunkQueueMessage(
                    job_id=job_id,
                    batch_index=i,
                    attempt=0,
                    enqueued_at=time.time(),
                )
                for i in range(len(batches))
            ]
            rpush_chunk_messages(msgs)
            logger.info(
                "prepare_document_chunk_jobs repaired queue job=%s batches=%s",
                job_id,
                len(batches),
            )
            return len(batches), time.perf_counter() - t0, breakdown
        except Exception:
            logger.exception("prepare repair failed job=%s; rebuilding", job_id)

    settings = get_pipeline_settings()
    tt = normalize_translation_target(translation_target)
    if blocks is not None:
        breakdown["parse_second_document_total_s"] = 0.0
        breakdown["parse_second_skipped_reused_blocks"] = 1.0
        logger.info(
            "chunk_prepare stage=parse skipped reused_blocks=%s job=%s",
            len(blocks),
            job_id,
        )
    else:
        t_parse = time.perf_counter()
        parse_detail: dict[str, float] = {}
        blocks = parse_document(input_path, timings=parse_detail)
        parse_wall = time.perf_counter() - t_parse
        breakdown["parse_second_document_total_s"] = parse_wall
        breakdown.update({f"parse_second_{k}": v for k, v in parse_detail.items()})
        logger.info(
            "chunk_prepare stage=parse blocks=%s wall_s=%.3f job=%s",
            len(blocks),
            parse_wall,
            job_id,
        )

    t_cls = time.perf_counter()
    classified = classify_blocks(blocks)
    cls_wall = time.perf_counter() - t_cls
    breakdown["classify_s"] = cls_wall
    logger.info(
        "chunk_prepare stage=classify wall_s=%.3f job=%s",
        cls_wall,
        job_id,
    )

    t_plan = time.perf_counter()
    segments, block_work = build_translation_plan(classified)
    plan_wall = time.perf_counter() - t_plan
    breakdown["translation_plan_and_chunking_s"] = plan_wall
    logger.info(
        "chunk_prepare stage=plan segments=%s wall_s=%.3f job=%s",
        len(segments),
        plan_wall,
        job_id,
    )

    t_pack = time.perf_counter()
    if settings.chunk_queue_pack_by_tokens:
        batches = pack_segment_indices_by_tokens(
            segments,
            min_tokens_per_batch=settings.chunk_queue_min_tokens,
            max_tokens_per_batch=settings.chunk_queue_max_tokens,
        )
    else:
        batches = pack_segment_indices_by_words(
            segments,
            min_words_per_batch=settings.chunk_job_min_words,
            max_words_per_batch=settings.chunk_job_max_words,
        )
    pack_wall = time.perf_counter() - t_pack
    breakdown["word_pack_queue_batches_s"] = pack_wall
    logger.info(
        "chunk_prepare stage=word_pack batches=%s wall_s=%.3f job=%s",
        len(batches),
        pack_wall,
        job_id,
    )

    manifest = dump_manifest_v2(
        segments=segments,
        batches=batches,
        block_work=block_work,
        document_template_id=document_template_id,
        translation_target=tt,
    )
    manifest_path.write_bytes(manifest_json_bytes(manifest))

    if not batches:
        set_job_chunk_totals(job_id, 0)
        logger.info("chunk_prepare no API batches job=%s", job_id)
        total_nb = time.perf_counter() - t0
        breakdown["prepare_chunk_jobs_total_s"] = total_nb
        _log_chunk_prepare_perf(
            job_id,
            mode="no_api_segments",
            num_batches=0,
            num_segments=len(segments),
            breakdown=breakdown,
        )
        return 0, total_nb, breakdown

    set_job_chunk_totals(job_id, len(batches))
    for i in range(len(batches)):
        set_chunk_status(job_id, i, "pending")

    cap = int(getattr(settings, "inline_translation_max_batches", 0) or 0)
    if cap > 0 and len(batches) <= cap:
        breakdown["inline_translate_sequential_s"] = _inline_translate_batches(
            job_id,
            job_dir,
            segments,
            batches,
            translation_target=tt,
        )
        logger.info(
            "chunk_prepare stage=inline_done total_wall_s=%.3f job=%s",
            time.perf_counter() - t0,
            job_id,
        )
        total = time.perf_counter() - t0
        breakdown["prepare_chunk_jobs_total_s"] = total
        _log_chunk_prepare_perf(
            job_id,
            mode="inline_parallel",
            num_batches=len(batches),
            num_segments=len(segments),
            breakdown=breakdown,
        )
        return len(batches), total, breakdown

    enq = time.time()
    msgs = [
        ChunkQueueMessage(
            job_id=job_id,
            batch_index=i,
            attempt=0,
            enqueued_at=enq,
        )
        for i in range(len(batches))
    ]
    t_enq = time.perf_counter()
    rpush_chunk_messages(msgs)
    breakdown["redis_chunk_enqueue_s"] = time.perf_counter() - t_enq
    breakdown["prepare_chunk_jobs_total_s"] = time.perf_counter() - t0
    logger.info(
        "chunk_prepare stage=enqueued chunks=%s segments=%s total_wall_s=%.3f job=%s",
        len(msgs),
        len(segments),
        time.perf_counter() - t0,
        job_id,
    )
    _log_chunk_prepare_perf(
        job_id,
        mode="redis_chunk_queue",
        num_batches=len(batches),
        num_segments=len(segments),
        breakdown=breakdown,
    )
    return len(batches), time.perf_counter() - t0, breakdown
