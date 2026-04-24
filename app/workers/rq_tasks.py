"""RQ worker entry: prepare/shard, finalize, or monolithic pipeline for a `jobs` row."""

from __future__ import annotations

import json
import logging
import shutil
import threading
import time
import uuid
from typing import Literal
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import func, select

from app.core.pipeline_settings import get_pipeline_settings
from app.db.models import DocumentJob, JobStatus, Profile, UsageRecord
from app.db.session import get_session_factory
from app.jobs.chunk_queue import (
    get_job_chunk_counters,
    pipeline_perf_hgetall,
    pipeline_perf_hset_str,
    release_finalize_running,
    try_acquire_finalize_running,
    try_set_finalize_enqueued,
)
from app.jobs.job_progress import publish_translation_progress
from app.billing_constants import is_high_priority_plan
from app.jobs.rq_queue import enqueue_document_job, enqueue_finalize_translation_job
from app.observability.metrics import worker_job_metrics
from app.observability.pipeline_performance import (
    PipelinePerfReport,
    log_distributed_pipeline_report,
    merge_redis_perf_strings,
)
from app.services.document_chunk_prepare import prepare_document_chunk_jobs
from app.services.parser import parse_document
from app.services.pipeline_runner import (
    estimate_input_tokens_from_blocks,
    run_pipeline,
    try_convert_docx_to_pdf,
    write_translated_docx,
)
from app.services.translation_plan_serde import load_manifest_v2
from app.services.word_credits import (
    add_usage_row,
    apply_word_charge,
    compute_word_charge,
    refresh_subscription_expiry,
)
from app.utils.chunking import count_tokens
from app.utils.translation_output_filenames import translation_output_filename
from app.utils.zip_export import write_translation_zip

logger = logging.getLogger(__name__)


def _enqueue_retry_worker(job_id: str, user_id: uuid.UUID, *, mode: Literal["pipeline", "finalize"]) -> None:
    plan = "free"
    factory = get_session_factory()
    if factory is not None:
        with factory() as session:
            p = session.get(Profile, user_id)
            if p is not None and is_high_priority_plan(str(p.plan)):
                plan = "paid"

    def _go() -> None:
        if mode == "finalize":
            enqueue_finalize_translation_job(job_id)
        else:
            enqueue_document_job(job_id, plan=plan)

    threading.Thread(target=_go, daemon=True).start()


def _translation_attempt_retry_or_fail(
    *,
    session,
    jid: uuid.UUID,
    job: DocumentJob,
    err: Exception,
    started: float,
    where: str,
    mode: Literal["pipeline", "finalize"],
) -> bool:
    """Return True if a retry was scheduled (caller should not mark failed)."""
    settings = get_pipeline_settings()
    max_a = max(1, int(settings.translation_max_processing_attempts or 3))
    new_att = int(job.translation_attempt or 0) + 1
    if new_att < max_a:
        job.translation_attempt = new_att
        job.status = JobStatus.PENDING.value
        job.error_message = (f"{where} will retry {new_att}/{max_a}: {err!s}")[:4000]
        job.completed_at = None
        job.processing_time_seconds = None
        session.commit()
        _enqueue_retry_worker(str(jid), job.user_id, mode=mode)
        logger.warning(
            "scheduled translation retry job=%s attempt=%s/%s where=%s",
            jid,
            new_att,
            max_a,
            where,
        )
        return True
    job.status = JobStatus.FAILED.value
    job.error_message = str(err)[:4000]
    job.completed_at = datetime.now(timezone.utc)
    job.processing_time_seconds = time.perf_counter() - started
    session.commit()
    worker_job_metrics.record_failure()
    return False


def _merge_chunk_translations(
    job_dir: Path,
    *,
    segments_len: int,
    batches: list[list[int]],
) -> tuple[list[str], int]:
    if segments_len == 0:
        return [], 0
    out_dir = job_dir / "work" / "out"
    translations = [""] * segments_len
    usage_total = 0
    for bi, _idxs in enumerate(batches):
        p = out_dir / f"batch_{bi}.json"
        if not p.is_file():
            raise FileNotFoundError(f"Missing batch output: {p.name}")
        raw = json.loads(p.read_text(encoding="utf-8"))
        usage_total += int(raw.get("usage_tokens") or 0)
        ids = [int(x) for x in raw["indices"]]
        txs = list(raw["translations"])
        if len(ids) != len(txs):
            raise ValueError(f"batch {bi} indices/translations length mismatch")
        for i, tx in zip(ids, txs, strict=True):
            translations[i] = tx
    return translations, usage_total


def prepare_document_translation_job(job_id: str) -> None:
    """Parse/plan and push independent chunk jobs to Redis (chunk_worker consumes)."""
    factory = get_session_factory()
    if factory is None:
        logger.error("prepare_document_translation_job: database not configured")
        return
    try:
        jid = uuid.UUID(job_id)
    except ValueError:
        logger.warning("Invalid job id %s", job_id)
        return

    settings = get_pipeline_settings()
    started = time.perf_counter()

    with factory() as session:
        job = session.get(DocumentJob, jid)
        if job is None:
            logger.warning("Job %s not found", job_id)
            return
        if str(job.status) in (JobStatus.AWAITING_PAYMENT.value, "awaiting_payment"):
            logger.warning("Job %s awaiting payment; prepare skip", job_id)
            return
        if job.status == JobStatus.COMPLETED.value and job.output_file_path:
            logger.info("Job %s already completed; prepare skip", job_id)
            return

        job.status = JobStatus.PROCESSING.value
        session.commit()

        input_path = settings.data_dir / job.input_file_path
        job_dir = input_path.parent

        try:
            publish_translation_progress(
                str(job_id),
                progress_percent=8,
                current_stage="preparing",
            )
            rq_wait_s = 0.0
            if job.created_at is not None:
                ca = job.created_at
                if ca.tzinfo is None:
                    ca = ca.replace(tzinfo=timezone.utc)
                rq_wait_s = max(0.0, time.time() - ca.timestamp())

            parse_first_detail: dict[str, float] = {}
            t_first_parse = time.perf_counter()
            parse_stop = threading.Event()
            pulse_n = [10]

            def _parse_pulse() -> None:
                while not parse_stop.wait(timeout=1.5):
                    pulse_n[0] = min(18, pulse_n[0] + 1)
                    publish_translation_progress(
                        str(job_id),
                        progress_percent=pulse_n[0],
                        current_stage="parsing_document",
                    )

            pulse_th = threading.Thread(target=_parse_pulse, daemon=True)
            pulse_th.start()
            try:
                blocks = parse_document(input_path, timings=parse_first_detail)
            finally:
                parse_stop.set()
                pulse_th.join(timeout=2.0)
            first_parse_wall = time.perf_counter() - t_first_parse
            est = estimate_input_tokens_from_blocks(blocks)
            if est > settings.max_tokens_per_job:
                raise RuntimeError(
                    f"Document exceeds maximum translation size "
                    f"({settings.max_tokens_per_job} estimated tokens)."
                )

            publish_translation_progress(
                str(job_id),
                progress_percent=20,
                current_stage="chunking",
            )
            num_batches, prep_wall, prep_breakdown = prepare_document_chunk_jobs(
                job_id=str(job_id),
                input_path=input_path,
                job_dir=job_dir,
                blocks=blocks,
            )
            perf_map: dict[str, str] = {
                "rq_queue_wait_estimate_s": f"{rq_wait_s:.6f}",
                "prepare_first_parse_total_s": f"{first_parse_wall:.6f}",
                "prepare_worker_wall_s": f"{time.perf_counter() - started:.6f}",
                "prepare_completed_at_unix": f"{time.time():.6f}",
            }
            for k, v in parse_first_detail.items():
                perf_map[f"prepare_first_parse_{k}"] = f"{v:.6f}"
            for k, v in prep_breakdown.items():
                perf_map[f"prepare_{k}"] = f"{v:.6f}"
            pipeline_perf_hset_str(str(job_id), perf_map)
            sec_parse = prep_breakdown.get("parse_second_document_total_s")
            if first_parse_wall > 0.05 and sec_parse and sec_parse > 0.05:
                logger.info(
                    "perf_note job=%s duplicate_full_document_parse "
                    "first_s=%.3f second_s=%.3f (prepare path parses twice today).",
                    job_id,
                    first_parse_wall,
                    sec_parse,
                )

            logger.info(
                "Job %s prepare done batches=%s wall_s=%.3f est_tokens=%s rq_wait_s=%.3f",
                job_id,
                num_batches,
                prep_wall,
                est,
                rq_wait_s,
            )
            seg_total = 0
            mp = job_dir / "work" / "manifest.json"
            if mp.is_file():
                seg_total = len(json.loads(mp.read_text(encoding="utf-8")).get("segments", []))
            publish_translation_progress(
                job_id,
                progress_percent=25,
                current_stage="chunk_queued",
                batches_done=0,
                batches_total=max(1, num_batches) if num_batches else 1,
                segments_translated=0,
                segments_total=seg_total,
            )
            if num_batches == 0:
                publish_translation_progress(
                    job_id,
                    progress_percent=88,
                    current_stage="stitching",
                    batches_done=0,
                    batches_total=0,
                )
                if try_set_finalize_enqueued(job_id):
                    enqueue_finalize_translation_job(job_id)
        except Exception as e:
            logger.exception("Job %s prepare failed", job_id)
            session.rollback()
            job = session.get(DocumentJob, jid)
            if job:
                if not _translation_attempt_retry_or_fail(
                    session=session,
                    jid=jid,
                    job=job,
                    err=e,
                    started=started,
                    where="prepare",
                    mode="pipeline",
                ):
                    pass
            else:
                worker_job_metrics.record_failure()


def finalize_document_translation_job(job_id: str) -> None:
    """Merge chunk outputs, write DOCX/PDF, billing (idempotent)."""
    factory = get_session_factory()
    if factory is None:
        logger.error("finalize_document_translation_job: database not configured")
        return
    try:
        jid = uuid.UUID(job_id)
    except ValueError:
        logger.warning("Invalid job id %s", job_id)
        return

    settings = get_pipeline_settings()
    started = time.perf_counter()

    with factory() as session:
        job = session.get(DocumentJob, jid)
        if job is None:
            logger.warning("Job %s not found", job_id)
            return
        if job.status == JobStatus.COMPLETED.value and job.output_file_path:
            logger.info("Job %s already finalized; skip", job_id)
            return

        if not try_acquire_finalize_running(str(job_id)):
            logger.info("Finalize already running elsewhere for job %s", job_id)
            return

        input_path = settings.data_dir / job.input_file_path
        job_dir = input_path.parent
        manifest_path = job_dir / "work" / "manifest.json"
        if not manifest_path.is_file():
            job.status = JobStatus.FAILED.value
            job.error_message = "Internal error: translation manifest missing."
            job.completed_at = datetime.now(timezone.utc)
            session.commit()
            release_finalize_running(str(job_id))
            return

        raw_man = json.loads(manifest_path.read_text(encoding="utf-8"))
        segments, batches, block_work = load_manifest_v2(raw_man)
        total, ok, fail = get_job_chunk_counters(job_id)

        if len(batches) > 0 and ok + fail < len(batches):
            logger.warning(
                "finalize: chunks not terminal yet job=%s ok=%s fail=%s total=%s",
                job_id,
                ok,
                fail,
                len(batches),
            )
            release_finalize_running(str(job_id))
            return

        if fail > 0:
            job.status = JobStatus.FAILED.value
            job.error_message = (
                "One or more translation chunks failed after retries. "
                "Try again or contact support."
            )[:4000]
            job.completed_at = datetime.now(timezone.utc)
            job.processing_time_seconds = time.perf_counter() - started
            session.commit()
            worker_job_metrics.record_failure()
            publish_translation_progress(
                job_id,
                progress_percent=0,
                current_stage="failed",
            )
            release_finalize_running(str(job_id))
            return

        total_tokens_used = 0

        try:
            publish_translation_progress(
                job_id,
                progress_percent=90,
                current_stage="generating_file",
            )
            finalize_started_unix = time.time()
            raw_perf = pipeline_perf_hgetall(job_id)
            fvals = merge_redis_perf_strings(raw_perf)
            prep_at = fvals.get("prepare_completed_at_unix")
            parallel_span_s = 0.0
            if prep_at is not None:
                parallel_span_s = max(0.0, finalize_started_unix - prep_at)

            t_merge = time.perf_counter()
            merged, usage_sum = _merge_chunk_translations(
                job_dir,
                segments_len=len(segments),
                batches=batches,
            )
            merge_s = time.perf_counter() - t_merge
            total_tokens_used = usage_sum

            upload_name = job.input_filename or "upload"
            final_docx = job_dir / translation_output_filename(upload_name, "docx")
            t_write = time.perf_counter()
            write_translated_docx(
                input_path,
                block_work=block_work,
                translated_segments=merged,
                output_docx=final_docx,
            )
            docx_s = time.perf_counter() - t_write
            logger.info(
                "finalize stage=docx_write wall_s=%.3f job=%s",
                docx_s,
                job_id,
            )

            export = job.export_format.lower()
            pdf_s = 0.0
            if export == "pdf":
                t_pdf = time.perf_counter()
                pdf_path = try_convert_docx_to_pdf(final_docx)
                pdf_s = time.perf_counter() - t_pdf
                rel = pdf_path.relative_to(settings.data_dir)
                job.output_file_path = str(rel).replace("\\", "/")
            elif export == "both":
                t_pdf = time.perf_counter()
                pdf_path = try_convert_docx_to_pdf(final_docx)
                pdf_s = time.perf_counter() - t_pdf
                zip_path = job_dir / translation_output_filename(upload_name, "zip")
                write_translation_zip(
                    {
                        translation_output_filename(upload_name, "docx"): final_docx,
                        translation_output_filename(upload_name, "pdf"): pdf_path,
                    },
                    zip_path,
                )
                rel = zip_path.relative_to(settings.data_dir)
                job.output_file_path = str(rel).replace("\\", "/")
            else:
                rel = final_docx.relative_to(settings.data_dir)
                job.output_file_path = str(rel).replace("\\", "/")

            job.status = JobStatus.COMPLETED.value
            job.error_message = None
            job.completed_at = datetime.now(timezone.utc)
            job.processing_time_seconds = time.perf_counter() - started

            if total_tokens_used <= 0:
                total_tokens_used = _count_input_tokens_fallback(input_path)

            words = max(0, int(round(total_tokens_used * 0.75)))
            job.tokens_used = words

            profile = session.get(Profile, job.user_id)
            if profile is None:
                raise RuntimeError("Profile not found for job.")
            refresh_subscription_expiry(profile)
            b = compute_word_charge(profile, words)
            payg = float(b.amount_to_pay or 0)
            bal = float(profile.credits_inr_balance or 0)
            if settings.payg_checkout_required and payg > bal + 1e-9:
                raise RuntimeError(
                    f"Insufficient pay-as-you-go credit for this job: "
                    f"need ₹{payg:.2f}, have ₹{bal:.2f}."
                )
            apply_word_charge(session, profile, b)
            job.cost_inr = payg  # type: ignore[assignment]
            add_usage_row(
                session,
                user_id=job.user_id,
                job_id=job.id,
                word_units=b.total_words,
                payg_inr=b.amount_to_pay,
            )

            session.commit()

            worker_job_metrics.record_success(
                time.perf_counter() - started,
                total_tokens_used,
            )
            publish_translation_progress(
                job_id,
                progress_percent=100,
                current_stage="completed",
            )
            def _is_duration_metric(key: str) -> bool:
                if key == "prepare_completed_at_unix" or key == "chunk_batches_completed":
                    return False
                return key.endswith("_s") or "_sum_s" in key or "_span_s" in key

            perf_stages = {
                k: float(v)
                for k, v in fvals.items()
                if _is_duration_metric(k) and isinstance(v, (int, float))
            }
            perf_stages["finalize_merge_translations_s"] = merge_s
            perf_stages["finalize_formatting_docx_s"] = docx_s
            perf_stages["finalize_pdf_export_s"] = pdf_s
            perf_stages["finalize_chunk_phase_wall_span_s"] = parallel_span_s

            dist_notes: list[str] = []
            batches_done = int(fvals.get("chunk_batches_completed") or 0)
            if batches_done > 0:
                qw_sum = float(fvals.get("chunk_queue_wait_sum_s") or 0.0)
                api_sum = float(fvals.get("chunk_openai_api_sum_s") or 0.0)
                dist_notes.append(
                    f"Chunk queue: batches={batches_done} sum(queue_wait_s)={qw_sum:.3f} "
                    f"avg_queue_wait_s={qw_sum / batches_done:.3f} sum(openai_api_s)={api_sum:.3f}"
                )
                if parallel_span_s > 0:
                    dist_notes.append(
                        f"Chunk phase wall span~{parallel_span_s:.2f}s vs sum(openai_api_s)~{api_sum:.2f}s "
                        f"(ratio~{api_sum / max(parallel_span_s, 1e-6):.2f}; >1 means some batch overlap)."
                    )
            p1 = fvals.get("prepare_first_parse_total_s")
            p2 = fvals.get("prepare_parse_second_document_total_s")
            if p1 and p2 and p1 > 0.05 and p2 > 0.05:
                dist_notes.append(
                    "Inefficiency: sharded prepare runs two full document parses "
                    f"(first~{p1:.2f}s, second~{p2:.2f}s); blocks from the first parse are discarded."
                )
            inline_tl = fvals.get("prepare_inline_translate_sequential_s")
            if inline_tl and inline_tl > 0:
                dist_notes.append(
                    f"Inline translation path used: API batches ran sequentially in prepare "
                    f"(~{inline_tl:.2f}s total); no Redis chunk queue concurrency for this job."
                )

            e2e_wall = None
            if job.created_at is not None:
                ca = job.created_at
                if ca.tzinfo is None:
                    ca = ca.replace(tzinfo=timezone.utc)
                e2e_wall = max(0.0, time.time() - ca.timestamp())

            log_distributed_pipeline_report(
                job_id=job_id,
                combined_stages=perf_stages,
                meta={
                    "pipeline_mode": "sharded_chunk_queue",
                    "approx_export": export,
                    "chunk_job_min_words": settings.chunk_job_min_words,
                    "chunk_job_max_words": settings.chunk_job_max_words,
                    "translate_global_max_inflight": settings.translate_global_max_inflight,
                    "chunk_worker_parallel_handlers": settings.chunk_worker_parallel_handlers,
                },
                notes=dist_notes,
                e2e_wall_s=e2e_wall,
            )

            logger.info(
                "Job %s finalized words=%s payg_inr=%s seconds=%.2f",
                job_id,
                words,
                payg,
                time.perf_counter() - started,
            )
        except Exception as e:
            logger.exception("Job %s finalize failed", job_id)
            session.rollback()
            job = session.get(DocumentJob, jid)
            if job:
                if not _translation_attempt_retry_or_fail(
                    session=session,
                    jid=jid,
                    job=job,
                    err=e,
                    started=started,
                    where="finalize",
                    mode="finalize",
                ):
                    pass
            else:
                worker_job_metrics.record_failure()
        finally:
            release_finalize_running(str(job_id))


def _count_input_tokens_fallback(path) -> int:
    """Estimate tokens when parse fails (should be rare)."""
    total = 0
    try:
        blocks = parse_document(path)
    except Exception as e:
        logger.warning("Token estimate failed, using 0: %s", e)
        return 0
    for b in blocks:
        if b.text:
            total += count_tokens(b.text)
        if b.data:
            for row in b.data:
                for cell in row:
                    if cell:
                        total += count_tokens(cell)
    return total


def process_document_job(job_id: str) -> None:
    factory = get_session_factory()
    if factory is None:
        logger.error("process_document_job: database not configured")
        return
    try:
        jid = uuid.UUID(job_id)
    except ValueError:
        logger.warning("Invalid job id %s", job_id)
        return

    settings = get_pipeline_settings()
    started = time.perf_counter()

    with factory() as session:
        job = session.get(DocumentJob, jid)
        if job is None:
            logger.warning("Job %s not found", job_id)
            return
        if str(job.status) in (JobStatus.AWAITING_PAYMENT.value, "awaiting_payment"):
            logger.warning("Job %s awaiting payment; worker skip", job_id)
            return
        if job.status == JobStatus.COMPLETED.value and job.output_file_path:
            logger.info("Job %s already completed; idempotent skip", job_id)
            return

        job.status = JobStatus.PROCESSING.value
        session.commit()

        input_path = settings.data_dir / job.input_file_path
        job_dir = input_path.parent

        total_tokens_used = 0

        def on_tokens(n: int) -> None:
            nonlocal total_tokens_used
            total_tokens_used += n

        try:
            perf = PipelinePerfReport(job_id=str(job_id))
            perf.meta["pipeline_mode"] = "monolithic_rq_worker"
            parse_timings: dict[str, float] = {}
            blocks = parse_document(input_path, timings=parse_timings)
            perf.merge_timings(parse_timings)
            est = estimate_input_tokens_from_blocks(blocks)
            if est > settings.max_tokens_per_job:
                raise RuntimeError(
                    f"Document exceeds maximum translation size "
                    f"({settings.max_tokens_per_job} estimated tokens)."
                )

            logger.info(
                "Job %s lifecycle: processing started input=%s est_tokens=%s blocks=%d",
                job_id,
                job.input_file_path,
                est,
                len(blocks),
            )

            docx_out = run_pipeline(
                input_path,
                blocks=blocks,
                on_tokens=on_tokens,
                progress_job_id=str(job_id),
                perf_report=perf,
            )
            upload_name = job.input_filename or "upload"
            final_docx = job_dir / translation_output_filename(upload_name, "docx")
            shutil.move(str(docx_out), str(final_docx))

            export = job.export_format.lower()
            if export == "pdf":
                pdf_path = try_convert_docx_to_pdf(final_docx)
                rel = pdf_path.relative_to(settings.data_dir)
                job.output_file_path = str(rel).replace("\\", "/")
            elif export == "both":
                pdf_path = try_convert_docx_to_pdf(final_docx)
                zip_path = job_dir / translation_output_filename(upload_name, "zip")
                write_translation_zip(
                    {
                        translation_output_filename(upload_name, "docx"): final_docx,
                        translation_output_filename(upload_name, "pdf"): pdf_path,
                    },
                    zip_path,
                )
                rel = zip_path.relative_to(settings.data_dir)
                job.output_file_path = str(rel).replace("\\", "/")
            else:
                rel = final_docx.relative_to(settings.data_dir)
                job.output_file_path = str(rel).replace("\\", "/")

            job.status = JobStatus.COMPLETED.value
            job.error_message = None
            job.completed_at = datetime.now(timezone.utc)
            elapsed = time.perf_counter() - started
            job.processing_time_seconds = elapsed

            if total_tokens_used <= 0:
                total_tokens_used = _count_input_tokens_fallback(input_path)

            words = max(0, int(round(total_tokens_used * 0.75)))
            job.tokens_used = words

            profile = session.get(Profile, job.user_id)
            if profile is None:
                raise RuntimeError("Profile not found for job.")
            refresh_subscription_expiry(profile)
            b = compute_word_charge(profile, words)
            payg = float(b.amount_to_pay or 0)
            bal = float(profile.credits_inr_balance or 0)
            if (
                settings.payg_checkout_required
                and payg > bal + 1e-9
            ):
                raise RuntimeError(
                    f"Insufficient pay-as-you-go credit for this job: "
                    f"need ₹{payg:.2f}, have ₹{bal:.2f}."
                )
            apply_word_charge(session, profile, b)
            job.cost_inr = payg  # type: ignore[assignment]
            add_usage_row(
                session,
                user_id=job.user_id,
                job_id=job.id,
                word_units=b.total_words,
                payg_inr=b.amount_to_pay,
            )

            session.commit()

            worker_job_metrics.record_success(
                elapsed,
                total_tokens_used,
            )
            logger.info(
                "Job %s lifecycle: completed words=%s payg_inr=%s seconds=%.2f",
                job_id,
                words,
                payg,
                elapsed,
            )
        except Exception as e:
            logger.exception("Job %s failed", job_id)
            session.rollback()
            job = session.get(DocumentJob, jid)
            if job:
                if not _translation_attempt_retry_or_fail(
                    session=session,
                    jid=jid,
                    job=job,
                    err=e,
                    started=started,
                    where="monolithic_pipeline",
                    mode="pipeline",
                ):
                    pass
            else:
                worker_job_metrics.record_failure()


def process_document(file_path: str, job_id: str) -> None:
    """Alternate entry name; ``file_path`` is ignored (input path comes from the ``jobs`` row)."""
    _ = file_path
    process_document_job(job_id)


def monthly_tokens_for_user(session, user_id: uuid.UUID) -> int:
    """Sum tokens from usage in rolling 30 days (approximate free-tier gate)."""
    since = datetime.now(timezone.utc) - timedelta(days=30)
    q = select(func.coalesce(func.sum(UsageRecord.tokens_used), 0)).where(
        UsageRecord.user_id == user_id,
        UsageRecord.created_at >= since,
    )
    return int(session.scalar(q) or 0)
