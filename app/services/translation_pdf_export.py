"""Translation export PDF: **WeasyPrint** from structured JSON (report-style HTML), then DOCX fallback.

**WeasyPrint vs DOCX→PDF:** WeasyPrint renders the same HTML+CSS path as the document templates (layout, fonts, chapter breaks from ``.doc-chapter-start``), so PDF matches themed structure. The DOCX→PDF fallback (LibreOffice / docx2pdf / etc.) lays out the Word file instead—useful when WeasyPrint is unavailable or fails on the host.
"""

from __future__ import annotations

import logging
from pathlib import Path

from app.models.structured_document import StructuredDocument

logger = logging.getLogger(__name__)


def export_translation_pdf(
    docx_path: Path,
    structure_json_path: Path | None,
    *,
    template_id: str | None = None,
    source_structured: StructuredDocument | None = None,
) -> Path:
    """
    Write ``docx_path.with_suffix('.pdf')`` using WeasyPrint when a sidecar
    ``*.structure.json`` exists; otherwise use LibreOffice / ReportLab / docx2pdf.

    WeasyPrint yields layout consistent with ``/api/document/html-to-pdf`` (Jinja themes).
    """
    pdf_path = docx_path.with_suffix(".pdf")
    if structure_json_path is not None and structure_json_path.is_file():
        try:
            return _pdf_via_weasyprint(
                structure_json_path,
                pdf_path,
                template_id=template_id,
                source_structured=source_structured,
            )
        except Exception as e:
            logger.warning(
                "WeasyPrint PDF from structure failed (%s); falling back to DOCX→PDF.",
                e,
                exc_info=logger.isEnabledFor(logging.DEBUG),
            )
    else:
        logger.info(
            "PDF: no structure sidecar at %s — chapter HTML/CSS (WeasyPrint) is skipped; "
            "using DOCX→PDF only (adds .structure.json next to the DOCX to enable WeasyPrint).",
            structure_json_path,
        )
    from app.services.pipeline_runner import try_convert_docx_to_pdf

    return try_convert_docx_to_pdf(docx_path)


def _pdf_via_weasyprint(
    structure_json_path: Path,
    pdf_path: Path,
    *,
    template_id: str | None,
    source_structured: StructuredDocument | None = None,
) -> Path:
    from app.services.document_template_render import init_document_template_render
    from app.services.formatter.html_to_pdf_weasyprint import html_to_pdf_bytes
    from app.services.structured_to_template import render_structured_document_html

    init_document_template_render()
    raw = structure_json_path.read_text(encoding="utf-8")
    doc = StructuredDocument.model_validate_json(raw)
    html = render_structured_document_html(
        doc,
        template_id,
        source_doc=source_structured,
    )
    pdf_path.write_bytes(html_to_pdf_bytes(html))
    logger.info("Translation PDF via WeasyPrint → %s", pdf_path)
    return pdf_path.resolve()
