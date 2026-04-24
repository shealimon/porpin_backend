"""Backward-compatible alias for the RQ worker task."""

from app.workers.rq_tasks import process_document_job as process_translation_job

__all__ = ["process_translation_job"]
