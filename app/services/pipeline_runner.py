"""Orchestrates: parse → classify → global batched translate → rebuild (+ optional PDF)."""

from __future__ import annotations

import logging
import math
import shutil
import subprocess
import tempfile
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
from app.services.classifier.section_classifier import classify_blocks
from app.services.formatter.document_builder import build_docx
from app.services.formatter.document_inplace import apply_translations_inplace
from app.services.parser import parse_document
from app.services.translation_plan import (
    BlockWork,
    build_translation_plan,
    reassemble_from_plan,
)
from app.services.translator.batch_translator import (
    pack_segment_indices,
    translate_segments_batched_sync,
)
from app.utils.chunking import count_tokens

logger = logging.getLogger(__name__)


def estimate_input_tokens_from_blocks(blocks: list[ContentBlock]) -> int:
    """Token estimate from parsed blocks (no I/O)."""
    total = 0
    for b in blocks:
        if b.text:
            total += count_tokens(b.text)
        if b.data:
            for row in b.data:
                for cell in row:
                    if cell:
                        total += count_tokens(cell)
    return total


def estimate_input_tokens(path: Path) -> int:
    """Rough input token count for job caps (parse + tiktoken)."""
    return estimate_input_tokens_from_blocks(parse_document(path))


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


def run_pipeline(
    input_path: Path,
    *,
    blocks: list[ContentBlock] | None = None,
    on_progress: Callable[[int], None] | None = None,
    on_tokens: Callable[[int], None] | None = None,
    progress_job_id: str | None = None,
    perf_report: PipelinePerfReport | None = None,
) -> Path:
    """Parse → classify → translate (async multi-segment batches) → rebuild DOCX."""

    def bump(pct: int) -> None:
        if on_progress is not None:
            on_progress(pct)

    wall_pipeline = time.perf_counter()
    report = perf_report or PipelinePerfReport(job_id=progress_job_id)
    report.meta.setdefault("input_path_suffix", input_path.suffix.lower())
    report.meta.setdefault("pipeline_mode", "monolithic_inproc")

    if blocks is None:
        t_parse = time.perf_counter()
        logger.info("Pipeline: parse starting path=%s", input_path)
        parse_timings: dict[str, float] = {}
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
            blocks = parse_document(input_path, timings=parse_timings)
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
    logger.info("Pipeline: classify (%d blocks)", len(blocks))
    classified = classify_blocks(blocks)
    classify_s = time.perf_counter() - t_classify
    report.add("classify_s", classify_s)
    logger.info(
        "stage=classify blocks=%s wall_s=%.3f",
        len(classified),
        classify_s,
    )
    bump(22)

    t_plan = time.perf_counter()
    global_jobs, block_work = build_translation_plan(classified)
    plan_s = time.perf_counter() - t_plan
    report.add("translation_plan_and_chunking_s", plan_s)
    logger.info(
        "stage=plan_build api_segments=%s wall_s=%.3f",
        len(global_jobs),
        plan_s,
    )

    settings = get_pipeline_settings()
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
        )

    report.meta["api_segment_count"] = len(global_jobs)
    report.meta["translate_batch_count"] = batch_count

    if not global_jobs:
        bump(88)
        translated_classified = classified
        logger.info("stage=translate skipped (no API segments)")
    else:
        n_seg = len(global_jobs)
        api_batch_wall_accum: list[float] = [0.0]
        # Progress is batch-driven; a single large API call would freeze the bar for ~tens of seconds.
        # Pulse fills 24%..~82% smoothly from elapsed time until batch callbacks jump to real %.
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
            )
        except Exception as e:
            logger.exception("Translation failed")
            raise RuntimeError(f"Translation failed: {e}") from e
        translate_wall = time.perf_counter() - t_tr
        report.add("translate_wall_clock_s", translate_wall)
        report.add("translation_api_per_batch_wall_sum_s", api_batch_wall_accum[0])
        logger.info(
            "stage=translate_http segments=%s wall_s=%.3f api_batch_wall_sum_s=%.3f",
            n_seg,
            translate_wall,
            api_batch_wall_accum[0],
        )

        t_re = time.perf_counter()
        translated_classified = reassemble_from_plan(block_work, translated_texts)
        reassemble_s = time.perf_counter() - t_re
        report.add("response_aggregation_reassemble_s", reassemble_s)
        logger.info("stage=reassemble wall_s=%.3f", reassemble_s)
        bump(88)

    if progress_job_id:
        publish_translation_progress(
            progress_job_id,
            progress_percent=90,
            current_stage="generating_file",
            segments_translated=len(global_jobs),
            segments_total=max(1, len(global_jobs)),
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
                    segments_translated=len(global_jobs),
                    segments_total=max(1, len(global_jobs)),
                )

    docx_pulse_th: threading.Thread | None = None
    if on_progress is not None or progress_job_id:
        docx_pulse_th = threading.Thread(target=_docx_write_pulse_loop, daemon=True)
        docx_pulse_th.start()
    t_write = time.perf_counter()
    try:
        if input_path.suffix.lower() == ".docx" and settings.use_docx_inplace:
            logger.info("Pipeline: in-place DOCX -> %s", out)
            apply_translations_inplace(input_path, translated_classified, out)
            report.meta["formatting_mode"] = "docx_inplace"
        else:
            logger.info("Pipeline: rebuild -> %s", out)
            build_docx(translated_classified, out)
            report.meta["formatting_mode"] = "docx_rebuild"
    finally:
        if docx_pulse_th is not None:
            docx_pulse_stop.set()
            docx_pulse_th.join(timeout=1.0)
    fmt_s = time.perf_counter() - t_write
    report.add("formatting_reconstruction_s", fmt_s)
    logger.info("stage=docx_write wall_s=%.3f", fmt_s)
    bump(96)
    if progress_job_id:
        publish_translation_progress(
            progress_job_id,
            progress_percent=96,
            current_stage="docx_ready",
            segments_translated=len(global_jobs),
            segments_total=max(1, len(global_jobs)),
        )
    e2e = time.perf_counter() - wall_pipeline
    logger.info(
        "stage=pipeline_total wall_s=%.3f path=%s",
        e2e,
        input_path,
    )
    pn = analyze_parallelism_note(
        translate_wall_s=report.stages.get("translate_wall_clock_s"),
        translate_api_sum_s=report.stages.get("translation_api_per_batch_wall_sum_s"),
        translate_batch_max_concurrency=settings.translate_batch_max_concurrency,
    )
    if pn:
        report.note(pn)
    report.meta["translate_batch_max_concurrency"] = settings.translate_batch_max_concurrency
    report.log_structured(e2e_wall_s=e2e, log=logger)
    return out


def write_translated_docx(
    input_path: Path,
    *,
    block_work: list[BlockWork],
    translated_segments: list[str],
    output_docx: Path,
) -> None:
    """Reassemble classified blocks and write DOCX (in-place or rebuild)."""
    translated_classified = reassemble_from_plan(block_work, translated_segments)
    settings = get_pipeline_settings()
    if input_path.suffix.lower() == ".docx" and settings.use_docx_inplace:
        apply_translations_inplace(input_path, translated_classified, output_docx)
    else:
        build_docx(translated_classified, output_docx)


def try_convert_docx_to_pdf(docx_path: Path) -> Path:
    """Best-effort DOCX→PDF; raises RuntimeError if no converter available."""
    pdf_path = docx_path.with_suffix(".pdf")

    # 1) LibreOffice — good fidelity when installed; fails fast if not.
    soffice = _find_soffice()
    if soffice:
        tmp = tempfile.mkdtemp(prefix="translator_pdf_")
        try:
            cmd = [
                str(soffice),
                "--headless",
                "--convert-to",
                "pdf",
                "--outdir",
                tmp,
                str(docx_path),
            ]
            subprocess.run(cmd, check=True, capture_output=True, timeout=120)
            produced = Path(tmp) / (docx_path.stem + ".pdf")
            if produced.is_file():
                produced.replace(pdf_path)
                logger.info("DOCX→PDF via LibreOffice → %s", pdf_path)
                return pdf_path.resolve()
        except Exception as e:
            logger.debug("LibreOffice convert failed: %s", e)

    # 2) ReportLab — pure Python; works without Word (docx2pdf can hang if Word is missing).
    try:
        from app.services.formatter.docx_to_pdf_reportlab import (
            convert_docx_to_pdf_reportlab,
        )

        convert_docx_to_pdf_reportlab(docx_path, pdf_path)
        if pdf_path.is_file():
            logger.info("DOCX->PDF via ReportLab -> %s", pdf_path)
            return pdf_path.resolve()
    except Exception as e:
        logger.warning("ReportLab PDF fallback failed: %s", e)

    # 3) Word COM (last): highest fidelity on Windows when Word is present.
    try:
        import docx2pdf  # type: ignore

        docx2pdf.convert(str(docx_path), str(pdf_path))
        if pdf_path.is_file():
            logger.info("DOCX->PDF via docx2pdf -> %s", pdf_path)
            return pdf_path.resolve()
    except Exception as e:
        logger.debug("docx2pdf failed: %s", e)

    raise RuntimeError(
        "PDF export failed: LibreOffice (soffice), ReportLab + system fonts, or Word + "
        "docx2pdf could not produce a PDF."
    )


def _find_soffice() -> Path | None:
    cfg = get_pipeline_settings().libreoffice_soffice_path
    if cfg:
        p = Path(cfg)
        if p.is_file():
            return p
    w = shutil.which("soffice") or shutil.which("soffice.exe")
    if w:
        return Path(w)
    for candidate in _libreoffice_soffice_candidates():
        if candidate.is_file():
            return candidate
    return None


def _libreoffice_soffice_candidates() -> list[Path]:
    """Common install locations when soffice is not on PATH (typical on Windows)."""
    import os
    import platform

    if platform.system() != "Windows":
        return []
    program_files = os.environ.get("ProgramFiles", r"C:\Program Files")
    program_files_x86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
    bases = [Path(program_files), Path(program_files_x86)]
    out: list[Path] = []
    for base in bases:
        out.append(base / "LibreOffice" / "program" / "soffice.exe")
        out.append(base / "OpenOffice 4" / "program" / "soffice.exe")
    return out
