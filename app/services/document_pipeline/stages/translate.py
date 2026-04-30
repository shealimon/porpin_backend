"""Stage 3: batch translate API segments and reassemble classified blocks."""

from __future__ import annotations

import logging
import math
import time
from collections.abc import Callable

from app.core.pipeline_settings import get_pipeline_settings
from app.jobs.job_progress import publish_translation_progress
from app.models.document_models import ClassifiedBlock
from app.services.translation_plan import build_translation_plan, reassemble_from_plan
from app.services.translation_target import (
    normalize_translation_target,
    translation_target_label,
)
from app.services.translator.batch_translator import (
    pack_segment_indices,
    translate_segments_batched_sync,
)

logger = logging.getLogger(__name__)


def translate_classified_blocks(
    classified: list[ClassifiedBlock],
    *,
    progress_job_id: str | None,
    on_progress: Callable[[int], None] | None,
    on_tokens: Callable[[int], None] | None,
    bump: Callable[[int], None],
    translation_target: str = "hinglish",
) -> tuple[list[ClassifiedBlock], dict[str, float], dict[str, int]]:
    """Run translation plan, API calls, and reassembly.

    Returns (translated_classified, perf_stages, meta) where ``meta`` has
    ``api_segment_count`` and ``translate_batch_count`` for observability.
    """
    settings = get_pipeline_settings()
    t_plan = time.perf_counter()
    global_jobs, block_work = build_translation_plan(classified)
    plan_s = time.perf_counter() - t_plan
    stage_timings: dict[str, float] = {
        "translation_plan_and_chunking_s": plan_s,
    }
    logger.info(
        "stage=plan_build api_segments=%s wall_s=%.3f",
        len(global_jobs),
        plan_s,
    )

    tgt = normalize_translation_target(translation_target)
    tgt_label = translation_target_label(tgt)

    batch_count = 0
    if global_jobs:
        budget = max(
            1500,
            int(settings.translate_api_batch_max_input_tokens)
            - int(settings.translate_api_batch_prompt_reserve_tokens),
        )
        seg_cap = max(8, int(settings.translate_api_batch_max_segments))
        batch_count = len(
            pack_segment_indices(
                global_jobs,
                max_batch_input_tokens=budget,
                max_segments_per_batch=seg_cap,
            )
        )

    if progress_job_id:
        publish_translation_progress(
            progress_job_id,
            progress_percent=24,
            current_stage="translating",
            batches_done=0,
            batches_total=max(1, batch_count),
            segments_translated=0,
            segments_total=len(global_jobs),
            translation_target=tgt,
            translation_target_label=tgt_label,
        )

    if not global_jobs:
        bump(88)
        logger.info("stage=translate skipped (no API segments)")
        stage_timings["translate_wall_clock_s"] = 0.0
        stage_timings["response_aggregation_reassemble_s"] = 0.0
        stage_timings["translation_api_per_batch_wall_sum_s"] = 0.0
        return classified, stage_timings, {
            "api_segment_count": 0,
            "translate_batch_count": batch_count,
        }

    n_seg = len(global_jobs)
    api_batch_wall_accum: list[float] = [0.0]
    prog_state: dict[str, int] = {
        "batch_pct": 24,
        "pulse_pct": 24,
        "batches_done": 0,
    }

    def _apply_translation_progress(display_pct: int) -> None:
        d = min(88, max(23, int(display_pct)))
        bump(d)
        if not progress_job_id:
            return
        bt = max(1, batch_count)
        bd = prog_state["batches_done"]
        approx_segments = (
            n_seg
            if bd >= bt
            else min(
                n_seg,
                int(n_seg * (d - 24) / max(1, 88 - 24)),
            )
        )
        publish_translation_progress(
            progress_job_id,
            progress_percent=d,
            current_stage="translating",
            batches_done=bd,
            batches_total=bt,
            segments_translated=approx_segments,
            segments_total=n_seg,
            translation_target=tgt,
            translation_target_label=tgt_label,
        )

    def on_translation_pulse(elapsed_s: float) -> None:
        span = 82 - 24
        cand = 24 + int(span * (1.0 - math.exp(-elapsed_s / 20.0)))
        prog_state["pulse_pct"] = max(prog_state["pulse_pct"], cand)
        merged = max(prog_state["batch_pct"], min(87, prog_state["pulse_pct"]))
        if merged > prog_state["batch_pct"] or prog_state["pulse_pct"] > 24:
            _apply_translation_progress(merged)

    def on_batch_done(done_batches: int, total_batches: int) -> None:
        if total_batches <= 0:
            return
        pct = 22 + int(66 * done_batches / total_batches)
        pct = min(88, max(23, pct))
        prog_state["batches_done"] = done_batches
        prog_state["batch_pct"] = max(prog_state["batch_pct"], pct)
        _apply_translation_progress(prog_state["batch_pct"])

    t_tr = time.perf_counter()
    logger.info("stage=translate_start segments=%s", n_seg)
    try:
        translated_texts = translate_segments_batched_sync(
            global_jobs,
            on_tokens=on_tokens,
            on_batch_done=on_batch_done,
            on_batch_timing=lambda _bi, _n, dt: api_batch_wall_accum.__setitem__(
                0, api_batch_wall_accum[0] + float(dt)
            ),
            on_translation_pulse=(
                on_translation_pulse
                if (progress_job_id or on_progress is not None)
                else None
            ),
            translation_target=tgt,
        )
    except Exception as e:
        logger.exception("Translation failed")
        raise RuntimeError(f"Translation failed: {e}") from e
    translate_wall = time.perf_counter() - t_tr
    logger.info(
        "stage=translate_http segments=%s wall_s=%.3f api_batch_wall_sum_s=%.3f",
        n_seg,
        translate_wall,
        api_batch_wall_accum[0],
    )

    t_re = time.perf_counter()
    translated_classified = reassemble_from_plan(block_work, translated_texts)
    reassemble_s = time.perf_counter() - t_re
    stage_timings["translate_wall_clock_s"] = translate_wall
    stage_timings["response_aggregation_reassemble_s"] = reassemble_s
    stage_timings["translation_api_per_batch_wall_sum_s"] = api_batch_wall_accum[0]
    logger.info("stage=reassemble wall_s=%.3f", reassemble_s)
    bump(88)
    return (
        translated_classified,
        stage_timings,
        {
            "api_segment_count": n_seg,
            "translate_batch_count": batch_count,
        },
    )
