"""GET /download/{job_id} — auth + free-tier gate + file stream."""

from __future__ import annotations

import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse

from app.core.pipeline_settings import get_pipeline_settings
from app.db.models import DocumentJob, JobStatus, Profile
from app.db.session import get_session_factory
from app.deps.supabase_auth import AuthProfile, require_auth_profile_flexible

router = APIRouter(tags=["download"])


@router.get("/download/{job_id}")
def download_translation(
    job_id: str,
    profile: AuthProfile = Depends(require_auth_profile_flexible),
):
    settings = get_pipeline_settings()
    factory = get_session_factory()
    if factory is None:
        raise HTTPException(status_code=503, detail="Database is not configured.")
    try:
        jid = uuid.UUID(job_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail="Invalid job id.") from e

    with factory() as session:
        job = session.get(DocumentJob, jid)
        if job is None:
            raise HTTPException(status_code=404, detail="Job not found.")
        if job.user_id != profile.id:
            raise HTTPException(status_code=403, detail="Not your job.")
        if job.status != JobStatus.COMPLETED.value or not job.output_file_path:
            raise HTTPException(status_code=404, detail="File not ready.")

        row = session.get(Profile, profile.id)
        bal = float(row.credits_inr_balance or 0) if row else float(profile.credits_inr_balance or 0)
        if bal < 0:
            raise HTTPException(
                status_code=402,
                detail="Account balance error; complete pay-as-you-go payment from Upload or contact support.",
            )

        path = settings.data_dir / job.output_file_path
        if not path.is_file():
            raise HTTPException(status_code=404, detail="Output missing on disk.")

        suffix = path.suffix.lower()
        media = "application/octet-stream"
        name = path.name
        if suffix == ".docx":
            media = (
                "application/vnd.openxmlformats-officedocument."
                "wordprocessingml.document"
            )
        elif suffix == ".pdf":
            media = "application/pdf"
        elif suffix == ".zip":
            media = "application/zip"

        return FileResponse(path=path, filename=name, media_type=media)
