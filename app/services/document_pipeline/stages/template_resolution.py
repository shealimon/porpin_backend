"""Stage 5: select presentation template (extensible allow-list).

Translation jobs that only emit DOCX can keep the default. PDF/HTML paths pass the same
identifier to :func:`app.services.document_template_render.render_document_html`.
"""

from __future__ import annotations

from app.services.document_template_render import (
    DEFAULT_DOCUMENT_TEMPLATE,
    resolve_template_type,
)


def resolve_document_template(requested: str | None) -> str:
    """Map user/API input to a known template id; unknown values fall back to default."""
    return resolve_template_type(requested)
