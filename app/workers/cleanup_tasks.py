"""Delete old job working directories from local storage."""

from __future__ import annotations

import logging
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import select

from app.core.pipeline_settings import get_pipeline_settings
from app.db.models import DocumentJob, JobStatus
from app.db.session import get_session_factory

logger = logging.getLogger(__name__)


def run_cleanup_pass() -> int:
    """Remove on-disk folders for completed jobs older than ``cleanup_ttl_minutes``."""
    settings = get_pipeline_settings()
    factory = get_session_factory()
    if factory is None:
        return 0
    cutoff = datetime.now(timezone.utc) - timedelta(
        minutes=settings.cleanup_ttl_minutes
    )
    removed = 0
    with factory() as session:
        q = select(DocumentJob).where(
            DocumentJob.status == JobStatus.COMPLETED.value,
            DocumentJob.completed_at.is_not(None),
            DocumentJob.completed_at < cutoff,
        )
        jobs = list(session.scalars(q))
        for job in jobs:
            try:
                base = Path(job.input_file_path).parent
                d = settings.data_dir / base
                if d.is_dir():
                    shutil.rmtree(d, ignore_errors=True)
                    removed += 1
                    logger.info("Cleanup removed job dir %s", d)
            except OSError as e:
                logger.warning("Cleanup skip %s: %s", job.id, e)
    return removed


def cleanup_loop_forever(interval_seconds: int = 3600) -> None:
    """Standalone process: run cleanup every ``interval_seconds``."""
    import time

    while True:
        try:
            n = run_cleanup_pass()
            if n:
                logger.info("Cleanup pass removed %d job dirs", n)
        except Exception:
            logger.exception("Cleanup pass failed")
        time.sleep(interval_seconds)
