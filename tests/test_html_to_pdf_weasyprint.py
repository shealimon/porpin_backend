"""WeasyPrint HTML→PDF smoke test (skipped when OS libraries are missing)."""

from __future__ import annotations

import pytest

from app.services.formatter.html_to_pdf_weasyprint import inject_print_css, safe_pdf_download_name
from app.services.document_template_render import render_document_html
from app.services.document_template_render.models import DocumentChapterModel, DocumentForTemplate
from app.models.structured_document import StructuredDocument, StructuredParagraph
from app.services.structured_to_template import structured_to_document_for_template
from app.services.document_template_render.context import build_template_context
from app.services.document_template_render.css import indented_css_for_template


def test_safe_pdf_download_name():
    assert safe_pdf_download_name("  My Report  ") == "My_Report.pdf"
    assert safe_pdf_download_name("x.pdf") == "x.pdf"
    assert safe_pdf_download_name(None) == "document.pdf"


def test_inject_print_css_inserts_before_head_close():
    html = "<!DOCTYPE html><html><head><meta charset=utf-8></head><body><p>a</p></body></html>"
    out = inject_print_css(html)
    assert "weasyprint-print" in out
    assert "@page" in out
    assert "@page :left" in out
    assert "string(chapter_title)" in out
    assert out.index("weasyprint-print") < out.lower().index("</head>")


def test_print_layout_css_a5_trim():
    from app.services.formatter.html_to_pdf_weasyprint import _print_layout_css

    assert "148mm 210mm" in _print_layout_css(page_size="a5")
    assert "size: A4" in _print_layout_css(page_size="a4")


def test_print_layout_css_classical_preset():
    from app.services.formatter.html_to_pdf_weasyprint import _print_layout_css

    css = _print_layout_css(book_preset="classical")
    assert "110mm 170mm" in css
    assert "@top-right" in css
    assert "font-variant: small-caps" in css


def test_inject_print_css_classical_running_heads():
    html = "<!DOCTYPE html><html><head><meta charset=utf-8></head><body><p>a</p></body></html>"
    out = inject_print_css(html, book_preset="classical")
    assert "@top-left" in out
    assert "@page :blank" in out

def test_template_chapter_opener_markup():
    model = DocumentForTemplate(
        title="My Book",
        chapters=[
            DocumentChapterModel(
                title="Always An Entrepreneur",
                paragraphs=["Challenging economic times bring out the entrepreneurial spirit..."],
            ),
            DocumentChapterModel(
                title="Second Chapter",
                paragraphs=["More content..."],
            ),
        ],
    )
    html = render_document_html(model, "ebook")
    assert 'class="doc-chapter-opener"' in html
    assert 'class="doc-chapter-number"' in html
    assert "Always An Entrepreneur" in html


def test_structured_to_template_dedup_consecutive_paragraphs():
    s = "Yeh bhukh swarth ki bhavna ko upar uthata hai."
    doc = StructuredDocument(
        title="t",
        content=[
            StructuredParagraph(text=s),
            StructuredParagraph(text=s),
            StructuredParagraph(text=s + "  "),  # whitespace-only differences
            StructuredParagraph(text="which he can cooperate, then he is"),
            StructuredParagraph(text=s),
        ],
    )
    model = structured_to_document_for_template(doc)
    paras = [b.text for b in (model.blocks or []) if getattr(b, "type", "") == "paragraph"]
    assert paras == [s, "which he can cooperate, then he is", s]


def test_template_context_falls_back_to_blocks_when_chapters_empty():
    model = DocumentForTemplate(
        title="t",
        chapters=[],
        blocks=[
            {"type": "paragraph", "text": "hello"},  # type: ignore[arg-type]
        ],
    )
    # DocumentForTemplate expects discriminated unions for blocks; supply via validate step.
    model = DocumentForTemplate.model_validate(model.model_dump())
    ctx = build_template_context(model, "ebook", indented_css_for_template("ebook"))
    assert ctx["use_chapters"] is False
    assert ctx["blocks"]


def test_template_context_falls_back_to_blocks_when_chapters_titles_only():
    model = DocumentForTemplate(
        title="t",
        chapters=[
            DocumentChapterModel(title="Chapter 4: The Pruning Shears of"),
            DocumentChapterModel(title="Chapter 5: Revision"),
        ],
        blocks=[
            {"type": "heading", "text": "Chapter 4: The Pruning Shears of", "level": 2, "chapter_start": True},  # type: ignore[arg-type]
            {"type": "paragraph", "text": "Body starts here."},  # type: ignore[arg-type]
        ],
    )
    model = DocumentForTemplate.model_validate(model.model_dump())
    ctx = build_template_context(model, "ebook", indented_css_for_template("ebook"))
    assert ctx["use_chapters"] is False
    assert ctx["blocks"]

def test_template_context_merges_consecutive_chapter_start_headings():
    model = DocumentForTemplate(
        title="t",
        blocks=[
            {"type": "heading", "text": "The Pruning Shears of", "level": 2, "chapter_start": True},  # type: ignore[arg-type]
            {"type": "heading", "text": "Revision", "level": 2, "chapter_start": True},  # type: ignore[arg-type]
            {"type": "paragraph", "text": "Body starts here."},  # type: ignore[arg-type]
        ],
    )
    model = DocumentForTemplate.model_validate(model.model_dump())
    ctx = build_template_context(model, "ebook", indented_css_for_template("ebook"))
    blocks = ctx["blocks"]
    # Only one chapter start heading should remain, with merged text.
    starts = [b for b in blocks if b.get("type") == "heading" and b.get("chapterStart")]
    assert len(starts) == 1
    assert "The Pruning Shears of" in starts[0]["text"]
    assert "Revision" in starts[0]["text"]


def test_html_to_pdf_bytes_smoke():
    try:
        from app.services.formatter.html_to_pdf_weasyprint import html_to_pdf_bytes
    except Exception:
        pytest.skip("WeasyPrint module unavailable")

    html = (
        "<!DOCTYPE html><html><head><meta charset=utf-8/>"
        "<style>body{font-family:sans-serif;font-size:12pt}</style></head>"
        "<body><p>Hello</p></body></html>"
    )
    try:
        out = html_to_pdf_bytes(html)
    except RuntimeError as e:
        if "WeasyPrint" in str(e) or "PDF rendering failed" in str(e):
            pytest.skip(str(e))
        raise
    assert isinstance(out, bytes)
    assert out[:4] == b"%PDF"
