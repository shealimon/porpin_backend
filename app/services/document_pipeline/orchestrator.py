"""Compose pipeline stages: extract → classify → translate → structure → export DOCX."""

from __future__ import annotations

import logging
import threading
import time
import uuid
from collections.abc import Callable
from pathlib import Path

from app.core.pipeline_settings import get_pipeline_settings
from app.jobs.job_progress import publish_translation_progress
from app.models.document_models import ContentBlock
from app.observability.pipeline_performance import (
    PipelinePerfReport,
    analyze_parallelism_note,
)
from app.services.document_pipeline.stages import (
    classify_source_blocks,
    extract_text_blocks,
    resolve_document_template,
)
from app.services.document_pipeline.stages.export_docx import (
    write_classified_to_docx,
    write_structured_sidecar,
)
from app.services.document_pipeline.stages.translate import translate_classified_blocks
from app.services.translation_target import normalize_translation_target
from app.services.structured_document_builder import (
    build_structured_document,
    structured_json_path_for_docx,
)

logger = logging.getLogger(__name__)


def _approx_word_count(blocks: list[ContentBlock]) -> int:
    import re

    n = 0
    for b in blocks:
        if b.text:
            n += len(re.findall(r"\S+", b.text))
        if b.data:
            for row in b.data:
                for cell in row:
                    if cell:
                        n += len(re.findall(r"\S+", cell))
    return n


def run_translate_export_docx_pipeline(
    input_path: Path,
    *,
    blocks: list[ContentBlock] | None = None,
    on_progress: Callable[[int], None] | None = None,
    on_tokens: Callable[[int], None] | None = None,
    progress_job_id: str | None = None,
    perf_report: PipelinePerfReport | None = None,
    template_id: str | None = None,
    translation_target: str = "hinglish",
) -> Path:
    """End-to-end job: **extract** → **classify** → **translate** → **structure** (metadata) → **DOCX export**.

    Template selection (``template_id``) is resolved and stored in ``perf_report.meta`` for downstream
    HTML/PDF; DOCX output still uses the python-docx formatter (stages 6–7 in the architecture doc).

    The structured JSON sidecar is written after export for API consumers; it does not require a
    separate render pass for DOCX.
    """
    def bump(pct: int) -> None:
        if on_progress is not None:
            on_progress(pct)

    wall_pipeline = time.perf_counter()
    report = perf_report or PipelinePerfReport(job_id=progress_job_id)
    report.meta.setdefault("input_path_suffix", input_path.suffix.lower())
    report.meta.setdefault("pipeline_mode", "monolithic_inproc")
    selected_template = resolve_document_template(template_id)
    report.meta["document_template_id"] = selected_template
    tt = normalize_translation_target(translation_target)
    report.meta["translation_target"] = tt

    parse_timings: dict[str, float] = {}
    if blocks is None:
        t_parse = time.perf_counter()
        logger.info("Pipeline: extract (parse) starting path=%s", input_path)
        parse_stop = threading.Event()
        pulse_val = [10]

        def _parse_pulse_loop() -> None:
            while not parse_stop.wait(timeout=1.5):
                pulse_val[0] = min(13, pulse_val[0] + 1)
                bump(pulse_val[0])
                if progress_job_id:
                    publish_translation_progress(
                        progress_job_id,
                        progress_percent=pulse_val[0],
                        current_stage="parsing_document",
                    )

        pulse_th: threading.Thread | None = None
        if on_progress is not None or progress_job_id:
            pulse_th = threading.Thread(target=_parse_pulse_loop, daemon=True)
            pulse_th.start()
        try:
            blocks = extract_text_blocks(
                input_path,
                timings=parse_timings,
            )
        except Exception as e:
            logger.exception("Parse failed")
            raise ValueError(f"Could not parse document: {e}") from e
        finally:
            if pulse_th is not None:
                parse_stop.set()
                pulse_th.join(timeout=1.0)
        report.merge_timings(parse_timings)
        logger.info(
            "stage=parse blocks=%s wall_s=%.3f",
            len(blocks),
            time.perf_counter() - t_parse,
        )
    else:
        logger.info(
            "stage=parse skipped using %d pre-parsed blocks for %s",
            len(blocks),
            input_path,
        )
    bump(14)
    report.meta["approx_source_words"] = _approx_word_count(blocks)

    t_classify = time.perf_counter()
    classified = classify_source_blocks(blocks)
    classify_s = time.perf_counter() - t_classify
    report.add("classify_s", classify_s)
    logger.info("stage=classify blocks=%s wall_s=%.3f", len(classified), classify_s)
    bump(22)

    settings = get_pipeline_settings()
    tr_classified, tr_timings, tr_meta = translate_classified_blocks(
        classified,
        progress_job_id=progress_job_id,
        on_progress=on_progress,
        on_tokens=on_tokens,
        bump=bump,
        translation_target=tt,
    )
    for k, v in tr_timings.items():
        report.add(k, v)
    report.meta["api_segment_count"] = tr_meta["api_segment_count"]
    report.meta["translate_batch_count"] = tr_meta["translate_batch_count"]
    global_jobs = tr_meta["api_segment_count"]
    if progress_job_id:
        publish_translation_progress(
            progress_job_id,
            progress_percent=90,
            current_stage="generating_file",
            segments_translated=global_jobs,
            segments_total=max(1, global_jobs),
        )

    out = settings.temp_dir / f"hinglish_{uuid.uuid4()}.docx"
    bump(92)
    docx_pulse_stop = threading.Event()
    docx_pulse_pct = [92]

    def _docx_write_pulse_loop() -> None:
        while not docx_pulse_stop.wait(timeout=0.35):
            docx_pulse_pct[0] = min(95, docx_pulse_pct[0] + 1)
            bump(docx_pulse_pct[0])
            if progress_job_id:
                publish_translation_progress(
                    progress_job_id,
                    progress_percent=docx_pulse_pct[0],
                    current_stage="writing_docx",
                    segments_translated=global_jobs,
                    segments_total=max(1, global_jobs),
                )

    docx_pulse_th: threading.Thread | None = None
    if on_progress is not None or progress_job_id:
        docx_pulse_th = threading.Thread(target=_docx_write_pulse_loop, daemon=True)
        docx_pulse_th.start()
    structured = build_structured_document(tr_classified)
    t_write = time.perf_counter()
    try:
        fmt = write_classified_to_docx(
            input_path,
            tr_classified,
            out,
            structured=structured,
        )
        report.meta["formatting_mode"] = fmt
    finally:
        if docx_pulse_th is not None:
            docx_pulse_stop.set()
            docx_pulse_th.join(timeout=1.0)
    try:
        write_structured_sidecar(structured, structured_json_path_for_docx(out))
    except Exception:
        logger.exception("Structured JSON export failed (DOCX still written)")
    fmt_s = time.perf_counter() - t_write
    report.add("formatting_reconstruction_s", fmt_s)
    logger.info("stage=docx_write wall_s=%.3f", fmt_s)
    bump(96)
    if progress_job_id:
        publish_translation_progress(
            progress_job_id,
            progress_percent=96,
            current_stage="docx_ready",
            segments_translated=global_jobs,
            segments_total=max(1, global_jobs),
        )
    e2e = time.perf_counter() - wall_pipeline
    logger.info("stage=pipeline_total wall_s=%.3f path=%s", e2e, input_path)
    tw = report.stages.get("translate_wall_clock_s")
    ts = report.stages.get("translation_api_per_batch_wall_sum_s")
    pn = analyze_parallelism_note(
        translate_wall_s=tw,
        translate_api_sum_s=ts,
        translate_batch_max_concurrency=settings.translate_batch_max_concurrency,
    )
    if pn:
        report.note(pn)
    report.meta["translate_batch_max_concurrency"] = settings.translate_batch_max_concurrency
    report.log_structured(e2e_wall_s=e2e, log=logger)
    return out


# Backwards-compatible alias
run_docx_translation_pipeline = run_translate_export_docx_pipeline
