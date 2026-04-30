"""Document output layer (isolated from auth, billing, and API surface).

Structured pipeline after translation::

    ClassifiedBlock[] → StructuredDocument (``structured_document_builder``)
    → HTML + theme CSS (``document_template_render`` / ``structured_to_template``)
    → PDF via WeasyPrint (``translation_pdf_export``)
    → DOCX via ``formatter.structured_docx_builder`` (structured data only; not HTML).

Rollback: set ``DOCX_REBUILD_FROM_STRUCTURE=false`` to use the legacy classified-block
DOCX rebuild path; PDF behavior is unchanged (structure JSON + WeasyPrint, then DOCX fallback).
"""

from app.services.formatter.structured_docx_builder import build_docx_from_structured
from app.services.structured_document_builder import build_structured_document
from app.services.translation_pdf_export import export_translation_pdf

__all__ = [
    "build_docx_from_structured",
    "build_structured_document",
    "export_translation_pdf",
]
