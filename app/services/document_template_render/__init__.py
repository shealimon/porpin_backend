"""Server-side HTML for document templates (ebook / report / minimal), matching the frontend model."""

from app.services.document_template_render.models import (
    DEFAULT_DOCUMENT_TEMPLATE,
    DocumentForTemplate,
    DocumentTemplateType,
    is_document_template_type,
)
from app.services.document_template_render.render import (
    init_document_template_render,
    render_document_html,
    resolve_template_type,
)

__all__ = [
    "DEFAULT_DOCUMENT_TEMPLATE",
    "DocumentForTemplate",
    "DocumentTemplateType",
    "is_document_template_type",
    "init_document_template_render",
    "render_document_html",
    "resolve_template_type",
]
