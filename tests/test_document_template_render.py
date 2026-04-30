"""Server-side document template (HTML) rendering."""

from app.services.document_template_render import (
    render_document_html,
    resolve_template_type,
)
from app.services.document_template_render.models import (
    BlockHeadingModel,
    BlockListModel,
    BlockParagraphModel,
    DocumentForTemplate,
)


def test_resolve_template_type_defaults_unknown_to_report():
    assert resolve_template_type(None) == "report"
    assert resolve_template_type("unknown") == "report"
    assert resolve_template_type("ebook") == "ebook"


def test_render_flat_blocks_applies_ebook_class():
    doc = DocumentForTemplate(
        title="T",
        paragraphs=["Hello"],
    )
    html = render_document_html(doc, template_id="ebook")
    assert "doc-theme--ebook" in html
    assert "Hello" in html


def test_render_sequenced_respects_block_order():
    doc = DocumentForTemplate(
        title="S",
        blocks=[
            {"type": "heading", "text": "A"},
            {"type": "paragraph", "text": "B"},
        ],
    )
    html = render_document_html(doc, "minimal")
    # heading before paragraph in output
    pos_h = html.index("A")
    pos_p = html.index("B")
    assert pos_h < pos_p


def test_render_heading_chapter_start_emits_css_class():
    doc = DocumentForTemplate(
        title="T",
        blocks=[
            BlockHeadingModel(text="Ch", level=3, chapter_start=True),
            BlockParagraphModel(text="Body"),
        ],
    )
    html = render_document_html(doc, "report")
    assert "doc-chapter-start" in html
    assert "doc-chapter-page" in html


def test_render_list_block_items_not_dict_method():
    """Jinja must use key 'items', not dict.items (builtin method)."""
    doc = DocumentForTemplate(
        title="L",
        blocks=[
            BlockListModel(items=["one", "two"], ordered=False),
        ],
    )
    html = render_document_html(doc, "report")
    assert "<ul" in html
    assert "one" in html and "two" in html


def test_template_type_none_uses_report_theme():
    doc = DocumentForTemplate(
        title="R",
        paragraphs=["x"],
    )
    html = render_document_html(doc, None)
    assert "doc-theme--report" in html
