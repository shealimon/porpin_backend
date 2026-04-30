"""Side-by-side source/target HTML for the ``bilingual`` theme (structured documents only)."""

from __future__ import annotations

from itertools import zip_longest

from app.models.structured_document import StructuredDocument
from app.services.document_template_render.context import _block_to_view
from app.services.document_template_render.css import indented_css_for_template
from app.services.document_template_render.models import (
    BlockParagraphModel,
    DocumentTemplateType,
)
from app.services.document_template_render.render import _get_jinja_env, init_document_template_render
from app.services.structured_to_template import structured_to_document_for_template


def render_bilingual_document_html(
    source: StructuredDocument,
    target: StructuredDocument,
    template_id: DocumentTemplateType,
) -> str:
    init_document_template_render()
    sm = structured_to_document_for_template(source)
    tm = structured_to_document_for_template(target)
    sb = sm.blocks or []
    tb = tm.blocks or []
    fill = BlockParagraphModel(text="\u00a0")
    pairs: list[dict[str, object]] = []
    for left, right in zip_longest(sb, tb, fillvalue=fill):
        pairs.append(
            {
                "source": _block_to_view(left),
                "target": _block_to_view(right),
            }
        )
    css = indented_css_for_template(template_id)
    ctx = {
        "title": tm.title,
        "themeId": template_id,
        "css": css,
        "use_chapters": False,
        "chapters": [],
        "blocks": [],
        "bilingual_pairs": pairs,
    }
    return _get_jinja_env().get_template("layout.j2").render(**ctx)
