"""Heuristics for section titles extracted from PDFs at same point size as body text."""

from app.models.document_models import BlockType
from app.services.parser.pdf_parser import (
    _looks_like_pdf_typographic_heading,
    _merge_short_paragraphs,
)


def test_pdf_caps_section_heading_detected_when_font_equals_body():
    assert _looks_like_pdf_typographic_heading(
        "INTRODUCTION",
        body_font=11.0,
        max_size=11.0,
    )
    assert not _looks_like_pdf_typographic_heading(
        "INTRODUCTION",
        body_font=11.0,
        max_size=14.5,
    )


def test_pdf_promotes_standalone_typographic_heading_paragraphs():
    from app.models.document_models import ContentBlock

    blocks = [
        ContentBlock(type=BlockType.PARAGRAPH, text="WORKS CITED"),
        ContentBlock(type=BlockType.PARAGRAPH, text="Foo bar baz qux."),
    ]
    out = _merge_short_paragraphs(blocks)
    assert out[0].type == BlockType.HEADING
    assert out[0].text == "WORKS CITED"


def test_pdf_all_caps_body_opening_not_heading():
    """Opening small-caps line + continuation must stay paragraph flow (see typographic books)."""
    assert not _looks_like_pdf_typographic_heading(
        (
            "THE GODFATHER IS ONE OF MY FAVORITE MOVIES, "
            "IN PART BECAUSE OF THE MANY"
        ),
        body_font=11.0,
        max_size=11.0,
    )


def test_pdf_all_caps_clause_end_not_heading():
    assert not _looks_like_pdf_typographic_heading(
        "THIS IS A SHORT LINE THAT ENDS WITH A COMMA,",
        body_font=11.0,
        max_size=11.0,
    )
