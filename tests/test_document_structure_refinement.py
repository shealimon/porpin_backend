"""Multi-signal document structure refinement (headings vs body)."""

from app.models.document_models import BlockType, ContentBlock
from app.services.parser.document_structure_refinement import refine_document_structure


def _p(text: str) -> ContentBlock:
    return ContentBlock(type=BlockType.PARAGRAPH, text=text)


def _h(text: str, level: int = 2) -> ContentBlock:
    return ContentBlock(type=BlockType.HEADING, text=text, level=level)


def test_refinement_does_not_promote_paragraphs_to_headings():
    """Structure refinement only demotes; it must not invent outline headings."""
    blocks = [
        _p("Body paragraph one."),
        _p("Judgment"),
        _p("Never Outshine the Master"),
        _p("More body."),
    ]
    refine_document_structure(blocks)
    assert blocks[1].type == BlockType.PARAGRAPH
    assert blocks[2].type == BlockType.PARAGRAPH


def test_inline_judgment_sentence_stays_paragraph():
    blocks = [_p("His judgment was correct and widely accepted.")]
    refine_document_structure(blocks)
    assert blocks[0].type == BlockType.PARAGRAPH


def test_false_all_caps_heading_demoted():
    blocks = [
        _h("THIS IS REALLY A LONG ALL CAPS SENTENCE THAT SHOULD NOT BE A HEADING"),
    ]
    refine_document_structure(blocks)
    assert blocks[0].type == BlockType.PARAGRAPH


def test_chapter_label_preserved_as_heading():
    blocks = [_h("Chapter 3: The River", level=2)]
    refine_document_structure(blocks)
    assert blocks[0].type == BlockType.HEADING
