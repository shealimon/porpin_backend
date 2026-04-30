"""POST /translate — async job enqueue (default) or legacy sync file response."""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from pathlib import Path

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    File,
    Form,
    Header,
    HTTPException,
    Query,
    Request,
    UploadFile,
)
from fastapi.responses import FileResponse, JSONResponse

from app.api.schemas.export_format import ExportFormat
from app.core.pipeline_settings import get_pipeline_settings
from app.db.models import User
from app.deps.auth import optional_api_key_user
from app.deps.quota import release_quota_slot, reserve_quota_slot
from app.deps.supabase_auth import resolve_auth_profile_with_anonymous_fallback
from app.limiter import limiter, user_or_ip_key
from app.observability.pipeline_performance import PipelinePerfReport
from app.services.async_job import create_and_enqueue_job_from_bytes
from app.services.pipeline_runner import run_pipeline
from app.services.translation_pdf_export import export_translation_pdf
from app.services.structured_document_builder import structured_json_path_for_docx
from app.services.storage.local_storage import save_temp_file
from app.utils.cleanup import safe_delete_many
from app.utils.file_validation import assert_allowed_filename, validate_file_bytes
from app.utils.translation_output_filenames import (
    translation_output_filename,
    translation_structure_output_filename,
)
from app.utils.zip_export import write_translation_zip

logger = logging.getLogger(__name__)

router = APIRouter()


def optional_user_only_when_sync_translate(
    x_api_key: str | None = Header(None, alias="X-API-Key"),
) -> User | None:
    """Skip DB lookup for API key user unless sync translate is enabled (quota path)."""
    if not get_pipeline_settings().enable_sync_translate:
        return None
    return optional_api_key_user(x_api_key=x_api_key)


def _enqueue_translate_job_sync2(
    authorization: str | None,
    x_api_key: str | None,
    filename: str,
    data: bytes,
    export: ExportFormat,
    deferred_payment: bool,
    payg_quote_inr: float | None,
    translation_target: str,
) -> tuple[uuid.UUID, str]:
    """Auth + disk/DB enqueue in one thread so the event loop stays responsive under load."""
    profile = resolve_auth_profile_with_anonymous_fallback(authorization, x_api_key)
    jid = create_and_enqueue_job_from_bytes(
        profile,
        filename,
        data,
        export,
        deferred_payment=deferred_payment,
        payg_quote_inr=payg_quote_inr,
        translation_target=translation_target,
    )
    st = "awaiting_payment" if deferred_payment else "pending"
    return jid, st


def _translate_rate_limit():
    settings = get_pipeline_settings()
    if settings.http_rate_limit_enabled:
        return limiter.limit(
            settings.http_translate_slowapi_limit,
            key_func=user_or_ip_key,
        )

    def noop(f):
        return f

    return noop


@router.post("/translate")
@_translate_rate_limit()
async def translate_document(
    request: Request,
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    export: ExportFormat = ExportFormat.DOCX,
    export_pdf: bool | None = None,
    deferred_payment: bool = Query(
        False,
        description="If true, store the file and return job_id; pay via /api/billing/razorpay/create-payg-translation-order + Checkout then verify.",
    ),
    payg_quote_inr: float | None = Form(
        default=None,
        description="Required when deferred_payment: estimated pay-as-you-go INR for the document (server matches Razorpay order amount).",
    ),
    translation_target: str = Form(
        default="hinglish",
        description="hinglish (Roman) or hindi (Devanagari).",
    ),
    user: User | None = Depends(optional_user_only_when_sync_translate),
):
    settings = get_pipeline_settings()
    if export_pdf is True:
        export = ExportFormat.PDF

    if settings.enable_sync_translate:
        return await _translate_sync(
            background_tasks=background_tasks,
            file=file,
            export=export,
            user=user,
        )

    data = await file.read()
    # Auth + DB + disk + Redis run in one worker thread (not on the event loop).
    job_id, payg_status = await asyncio.to_thread(
        _enqueue_translate_job_sync2,
        request.headers.get("Authorization"),
        request.headers.get("X-API-Key"),
        file.filename or "upload",
        data,
        export,
        deferred_payment,
        payg_quote_inr,
        translation_target,
    )
    # Mark response so clients do not treat JSON as DOCX/PDF binary (sync disabled by default).
    return JSONResponse(
        content={"job_id": str(job_id), "status": payg_status},
        headers={"X-Translator-Response": "async-job-enqueued"},
    )


async def _translate_sync(
    background_tasks: BackgroundTasks,
    file: UploadFile,
    export: ExportFormat,
    user: User | None,
) -> FileResponse:
    """Legacy: run pipeline in-process (dev / special cases)."""
    settings = get_pipeline_settings()
    if not settings.openai_api_key:
        raise HTTPException(
            status_code=503,
            detail="OPENAI_API_KEY is not configured.",
        )

    suffix = assert_allowed_filename(file.filename)

    perf = PipelinePerfReport(job_id="sync-http")
    t_up0 = time.perf_counter()
    data = await file.read()
    validate_file_bytes(suffix, data, max_bytes=settings.max_upload_bytes)
    input_path = save_temp_file(data, suffix)
    perf.add("upload_read_validate_save_s", time.perf_counter() - t_up0)

    reserved = False
    if user is not None:
        reserve_quota_slot(user)
        reserved = True

    paths_to_clean: list[Path] = [input_path]

    try:
        docx_out = run_pipeline(input_path, perf_report=perf)
        paths_to_clean.append(docx_out)
        paths_to_clean.append(structured_json_path_for_docx(docx_out))
    except ValueError as e:
        logger.warning("Parse / structure error: %s", e)
        safe_delete_many(paths_to_clean)
        if reserved and user is not None:
            release_quota_slot(user)
        raise HTTPException(status_code=422, detail=str(e)) from e
    except RuntimeError as e:
        logger.warning("Translation error: %s", e)
        safe_delete_many(paths_to_clean)
        if reserved and user is not None:
            release_quota_slot(user)
        raise HTTPException(status_code=502, detail=str(e)) from e
    except Exception as e:
        logger.exception("Unexpected pipeline failure")
        safe_delete_many(paths_to_clean)
        if reserved and user is not None:
            release_quota_slot(user)
        raise HTTPException(
            status_code=500,
            detail="Document processing failed.",
        ) from e

    upload_name = file.filename or "upload"
    response_path = docx_out
    media_type = (
        "application/vnd.openxmlformats-officedocument."
        "wordprocessingml.document"
    )
    download_name = translation_output_filename(upload_name, "docx")

    struct_path = structured_json_path_for_docx(docx_out)

    if export == ExportFormat.PDF:
        try:
            pdf_path = await asyncio.to_thread(
                export_translation_pdf,
                docx_out,
                struct_path,
            )
            paths_to_clean.append(pdf_path)
            response_path = pdf_path
            media_type = "application/pdf"
            download_name = translation_output_filename(upload_name, "pdf")
        except RuntimeError as e:
            safe_delete_many(paths_to_clean)
            if reserved and user is not None:
                release_quota_slot(user)
            raise HTTPException(status_code=501, detail=str(e)) from e
    elif export == ExportFormat.BOTH:
        try:
            pdf_path = await asyncio.to_thread(
                export_translation_pdf,
                docx_out,
                struct_path,
            )
            paths_to_clean.append(pdf_path)
        except RuntimeError as e:
            safe_delete_many(paths_to_clean)
            if reserved and user is not None:
                release_quota_slot(user)
            raise HTTPException(status_code=501, detail=str(e)) from e
        zip_path = settings.temp_dir / f"hinglish_bundle_{uuid.uuid4()}.zip"
        paths_to_clean.append(zip_path)
        docx_member = translation_output_filename(upload_name, "docx")
        pdf_member = translation_output_filename(upload_name, "pdf")
        write_translation_zip(
            {
                docx_member: docx_out,
                pdf_member: pdf_path,
                translation_structure_output_filename(upload_name): struct_path,
            },
            zip_path,
        )
        response_path = zip_path
        media_type = "application/zip"
        download_name = translation_output_filename(upload_name, "zip")

    background_tasks.add_task(safe_delete_many, paths_to_clean)

    return FileResponse(
        path=response_path,
        filename=download_name,
        media_type=media_type,
    )
