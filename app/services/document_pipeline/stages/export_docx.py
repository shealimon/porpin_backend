"""Stage 7 (export): persist translated content as DOCX and optional structured JSON sidecar."""

from __future__ import annotations

import logging
from pathlib import Path

from app.core.pipeline_settings import get_pipeline_settings
from app.models.document_models import ClassifiedBlock
from app.models.structured_document import StructuredDocument
from app.services.formatter.document_builder import build_docx
from app.services.formatter.document_inplace import apply_translations_inplace
from app.services.formatter.structured_docx_builder import build_docx_from_structured

logger = logging.getLogger(__name__)


def write_classified_to_docx(
    input_path: Path,
    translated_classified: list[ClassifiedBlock],
    output_docx: Path,
    *,
    structured: StructuredDocument | None = None,
) -> str:
    """Format mode used for metrics: ``docx_inplace`` or ``docx_rebuild``."""
    settings = get_pipeline_settings()
    if input_path.suffix.lower() == ".docx" and settings.use_docx_inplace:
        logger.info("Export: in-place DOCX -> %s", output_docx)
        apply_translations_inplace(input_path, translated_classified, output_docx)
        return "docx_inplace"
    logger.info("Export: rebuild DOCX -> %s", output_docx)
    if settings.docx_rebuild_from_structure and structured is not None:
        build_docx_from_structured(structured, output_docx)
    else:
        build_docx(translated_classified, output_docx)
    return "docx_rebuild"


def write_structured_sidecar(
    structured: StructuredDocument,
    structured_json_path: Path,
) -> None:
    structured_json_path.parent.mkdir(parents=True, exist_ok=True)
    structured_json_path.write_text(
        structured.model_dump_json(indent=2),
        encoding="utf-8",
    )
