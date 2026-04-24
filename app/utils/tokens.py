"""Short-lived JWTs for job downloads."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from jose import JWTError, jwt

from app.core.pipeline_settings import get_pipeline_settings


def create_download_token(job_id: str) -> str:
    settings = get_pipeline_settings()
    exp = datetime.now(timezone.utc) + timedelta(hours=1)
    return jwt.encode(
        {"sub": job_id, "exp": exp},
        settings.jwt_secret,
        algorithm="HS256",
    )


def verify_download_token(token: str, expected_job_id: str) -> bool:
    settings = get_pipeline_settings()
    try:
        payload = jwt.decode(
            token,
            settings.jwt_secret,
            algorithms=["HS256"],
        )
        return str(payload.get("sub")) == expected_job_id
    except JWTError:
        return False
