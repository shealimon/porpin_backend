"""Stage 4: normalized structured document (template-agnostic) for sidecars and HTML export."""

from __future__ import annotations

import logging

from app.models.document_models import ClassifiedBlock
from app.models.structured_document import StructuredDocument
from app.services.structured_document_builder import build_structured_document

logger = logging.getLogger(__name__)


def build_structured_payload(classified: list[ClassifiedBlock]) -> StructuredDocument:
    """Map translated classified blocks to :class:`StructuredDocument` (JSON-serializable)."""
    doc = build_structured_document(classified)
    logger.info("Structure: %d content blocks, title=%s", len(doc.content), bool(doc.title))
    return doc
