"""Glue PDF body paragraphs split across page breaks (reflow / two-up layouts)."""

from app.models.document_models import BlockType, ContentBlock
from app.services.parser.pdf_parser import (
    _merge_pdf_paragraphs_across_page_breaks,
    _looks_like_pdf_toc_leader_or_leaf,
    _pdf_text_looks_like_sentence_complete,
)


def test_cross_page_splits_joined_when_no_sentence_end():
    blocks = [
        ContentBlock(
            type=BlockType.PARAGRAPH,
            text=(
                "From early morning I had been oppressed by a"
            ),
            source_page=5,
        ),
        ContentBlock(
            type=BlockType.PARAGRAPH,
            text="strange despondency. It suddenly seemed to me that I was lonely.",
            source_page=6,
        ),
    ]
    out = _merge_pdf_paragraphs_across_page_breaks(blocks)
    assert len(out) == 1
    assert "oppressed by a strange despondency" in (out[0].text or "")
    assert out[0].source_page == 6


def test_cross_page_not_joined_when_sentence_complete():
    blocks = [
        ContentBlock(
            type=BlockType.PARAGRAPH,
            text="It was a wonderful night.",
            source_page=1,
        ),
        ContentBlock(
            type=BlockType.PARAGRAPH,
            text="The next day I walked in the Nevsky.",
            source_page=2,
        ),
    ]
    out = _merge_pdf_paragraphs_across_page_breaks(blocks)
    assert len(out) == 2


def test_toc_leader_not_glued_to_next_page_body():
    blocks = [
        ContentBlock(
            type=BlockType.PARAGRAPH,
            text="WHITE NIGHTS .......... 4",
            source_page=3,
        ),
        ContentBlock(
            type=BlockType.PARAGRAPH,
            text="FIRST NIGHT It was a wonderful night.",
            source_page=4,
        ),
    ]
    out = _merge_pdf_paragraphs_across_page_breaks(blocks)
    assert len(out) == 2


def test_hyphen_line_end_dehyphenates():
    blocks = [
        ContentBlock(type=BlockType.PARAGRAPH, text="We stud-", source_page=10),
        ContentBlock(type=BlockType.PARAGRAPH, text="ied the text.", source_page=11),
    ]
    out = _merge_pdf_paragraphs_across_page_breaks(blocks)
    assert len(out) == 1
    assert "studied" in (out[0].text or "").lower().replace(" ", "")


def test_chained_three_pages():
    blocks = [
        ContentBlock(type=BlockType.PARAGRAPH, text="Part one", source_page=1),
        ContentBlock(type=BlockType.PARAGRAPH, text="part two", source_page=2),
        ContentBlock(type=BlockType.PARAGRAPH, text="part three.", source_page=3),
    ]
    out = _merge_pdf_paragraphs_across_page_breaks(blocks)
    assert len(out) == 1
    assert "part one part two part three" in (out[0].text or "").lower()


def test_sentence_complete_detects_devanagari_danda():
    assert _pdf_text_looks_like_sentence_complete("कुछ वाक्य समाप्त।")
    assert not _pdf_text_looks_like_sentence_complete("अधूरा वाक्य जारी")


def test_toc_preface_leaf_not_body():
    assert _looks_like_pdf_toc_leader_or_leaf("Preface ... vii")
    assert not _looks_like_pdf_toc_leader_or_leaf("From early morning I had been oppressed by a")
