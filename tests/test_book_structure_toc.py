"""TOC region tagging vs Preface/Introduction body."""

from app.models.document_models import BlockType, ContentBlock, StructuralTag
from app.services.formatter.book_structure import apply_book_structure_tags


def test_toc_closes_when_preface_body_starts():
    blocks = [
        ContentBlock(type=BlockType.HEADING, text="Contents", level=1),
        ContentBlock(type=BlockType.PARAGRAPH, text="Preface ... vii"),
        ContentBlock(
            type=BlockType.PARAGRAPH,
            text=(
                "Preface I started working at an intelligence agency in August 2001. "
                "A few weeks later, the world changed forever."
            ),
        ),
    ]
    apply_book_structure_tags(blocks)
    assert blocks[0].structural_tag == StructuralTag.TOC
    assert blocks[1].structural_tag == StructuralTag.TOC
    assert blocks[2].structural_tag is None


def test_toc_keeps_introduction_row_with_leaf_page():
    blocks = [
        ContentBlock(type=BlockType.HEADING, text="Contents", level=1),
        ContentBlock(
            type=BlockType.PARAGRAPH,
            text="Introduction: The Power of Clear Thinking .......... 12",
        ),
    ]
    apply_book_structure_tags(blocks)
    assert blocks[0].structural_tag == StructuralTag.TOC
    assert blocks[1].structural_tag == StructuralTag.TOC


def test_imprint_line_does_not_become_toc_entry():
    blocks = [
        ContentBlock(type=BlockType.HEADING, text="Contents", level=1),
        ContentBlock(type=BlockType.PARAGRAPH, text="THE MACMILLAN COMPANY"),
        ContentBlock(type=BlockType.PARAGRAPH, text="FROM THE RUSSIAN BY CONSTANCE GARNETT"),
    ]
    apply_book_structure_tags(blocks)
    assert blocks[0].structural_tag == StructuralTag.TOC
    assert blocks[1].structural_tag is None
    assert blocks[2].structural_tag is None


def test_story_section_heading_closes_toc():
    blocks = [
        ContentBlock(type=BlockType.HEADING, text="Contents", level=1),
        ContentBlock(type=BlockType.PARAGRAPH, text="WHITE NIGHTS .......... 4"),
        ContentBlock(type=BlockType.HEADING, text="FIRST NIGHT", level=2),
    ]
    apply_book_structure_tags(blocks)
    assert blocks[0].structural_tag == StructuralTag.TOC
    assert blocks[1].structural_tag == StructuralTag.TOC
    assert blocks[2].structural_tag is None


def test_pdf_merge_gap_zero_keeps_paragraphs_separate():
    from app.services.parser.pdf_parser import _merge_short_paragraphs

    pg = 2
    blocks = [
        ContentBlock(type=BlockType.PARAGRAPH, text="Part 1 The enemies", source_page=pg),
        ContentBlock(type=BlockType.PARAGRAPH, text="1.1 Thinking badly", source_page=pg),
    ]
    out = _merge_short_paragraphs(blocks, max_gap=0)
    assert len(out) == 2
