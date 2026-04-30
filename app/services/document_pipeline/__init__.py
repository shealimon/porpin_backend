"""Document transformation pipeline: extract → classify → translate → structure → template → export.

Stages are split under ``stages/``; ``orchestrator`` composes the synchronous translation job flow.
For HTML/PDF, use :func:`app.services.structured_to_template.structured_to_document_for_template`
with :func:`app.services.document_template_render.render_document_html` after a ``StructuredDocument``
is available.
"""

from app.services.document_pipeline.orchestrator import run_translate_export_docx_pipeline
from app.services.document_template_render import DEFAULT_DOCUMENT_TEMPLATE
from app.services.document_pipeline.stages.template_resolution import resolve_document_template

__all__ = [
    "DEFAULT_DOCUMENT_TEMPLATE",
    "resolve_document_template",
    "run_translate_export_docx_pipeline",
]
