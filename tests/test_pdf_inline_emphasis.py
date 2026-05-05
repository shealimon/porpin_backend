"""All-caps dialogue/emphasis lines must stay inline paragraphs, not headings."""

from app.models.document_models import BlockType, ContentBlock
from app.services.parser.pdf_parser import (
    _looks_like_pdf_inline_emphasis_caps_fragment,
    _looks_like_pdf_typographic_heading,
    _merge_pdf_emphasis_heading_into_prior_paragraph,
)


def test_caps_shout_not_typographic_heading():
    assert _looks_like_pdf_inline_emphasis_caps_fragment(
        "MOMENT WHEN YOU NEED TO THINK!"
    )
    assert not _looks_like_pdf_typographic_heading(
        "MOMENT WHEN YOU NEED TO THINK!",
        body_font=11.0,
        max_size=11.0,
    )


def test_introduction_still_typographic_heading():
    assert not _looks_like_pdf_inline_emphasis_caps_fragment("INTRODUCTION")
    assert _looks_like_pdf_typographic_heading(
        "INTRODUCTION",
        body_font=11.0,
        max_size=11.0,
    )


def test_stitch_emphasis_heading_into_prior_paragraph():
    blocks = [
        ContentBlock(
            type=BlockType.PARAGRAPH,
            text="They say, 'STOP! THIS IS A",
            source_page=5,
        ),
        ContentBlock(
            type=BlockType.HEADING,
            text="MOMENT WHEN YOU NEED TO THINK!'",
            level=2,
            source_page=5,
        ),
    ]
    out = _merge_pdf_emphasis_heading_into_prior_paragraph(blocks)
    assert len(out) == 1
    assert "THIS IS A" in out[0].text
    assert "MOMENT WHEN YOU NEED TO THINK" in out[0].text


def test_no_stitch_after_terminal_period():
    blocks = [
        ContentBlock(type=BlockType.PARAGRAPH, text="The prior sentence ends.", source_page=1),
        ContentBlock(
            type=BlockType.HEADING,
            text="MOMENT WHEN YOU NEED TO THINK!",
            level=2,
            source_page=1,
        ),
    ]
    out = _merge_pdf_emphasis_heading_into_prior_paragraph(blocks)
    assert len(out) == 2
