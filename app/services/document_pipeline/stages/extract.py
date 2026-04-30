"""Stage 1: input file → logical content blocks (text extraction)."""

from __future__ import annotations

import logging
from pathlib import Path

from app.models.document_models import ContentBlock
from app.services.parser import parse_document

logger = logging.getLogger(__name__)


def extract_text_blocks(
    input_path: Path,
    *,
    timings: dict[str, float] | None = None,
    max_pdf_pages: int | None = None,
    max_preview_words: int | None = None,
) -> list[ContentBlock]:
    """Extract text as structured blocks (PDF, DOCX, EPUB, TXT, or raster image with OCR if configured)."""
    logger.info("Extract: parse path=%s", input_path)
    return parse_document(
        input_path,
        timings=timings,
        max_pdf_pages=max_pdf_pages,
        max_preview_words=max_preview_words,
    )
