"""Periodic deletion of old job working directories under ``data_dir/jobs``."""

from __future__ import annotations

import logging
import shutil
import time

from app.core.pipeline_settings import get_pipeline_settings

logger = logging.getLogger(__name__)


def cleanup_expired_job_files() -> int:
    """
    Remove job directories older than ``cleanup_ttl_minutes``.
    Returns number of top-level job dirs removed.
    """
    settings = get_pipeline_settings()
    ttl = settings.cleanup_ttl_minutes
    root = settings.data_dir / "jobs"
    if not root.is_dir():
        return 0
    cutoff = time.time() - ttl * 60
    removed = 0
    for child in root.iterdir():
        if not child.is_dir():
            continue
        try:
            mtime = child.stat().st_mtime
        except OSError:
            continue
        if mtime < cutoff:
            try:
                shutil.rmtree(child, ignore_errors=True)
                removed += 1
                logger.info("Cleaned old job dir %s", child)
            except OSError as e:
                logger.debug("Cleanup skip %s: %s", child, e)
    return removed
