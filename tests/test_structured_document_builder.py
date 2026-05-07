"""Structured document export from classified blocks."""

from app.models.document_models import (
    BlockType,
    ClassifiedBlock,
    ContentBlock,
    SectionAction,
    StructuralTag,
)
from app.services.structured_document_builder import build_structured_document


def test_build_structured_lifts_title_authors_and_preserves_order():
    classified = [
        ClassifiedBlock(
            block=ContentBlock(
                type=BlockType.HEADING,
                text="My Book",
                level=1,
                structural_tag=StructuralTag.TITLE,
            ),
            action=SectionAction.TRANSLATE,
        ),
        ClassifiedBlock(
            block=ContentBlock(
                type=BlockType.PARAGRAPH,
                text="Jane Doe",
                structural_tag=StructuralTag.AUTHOR,
            ),
            action=SectionAction.TRANSLATE,
        ),
        ClassifiedBlock(
            block=ContentBlock(type=BlockType.HEADING, text="Chapter One", level=1),
            action=SectionAction.TRANSLATE,
        ),
        ClassifiedBlock(
            block=ContentBlock(type=BlockType.PARAGRAPH, text="Body here."),
            action=SectionAction.TRANSLATE,
        ),
        ClassifiedBlock(
            block=ContentBlock(
                type=BlockType.LIST,
                text="one\ntwo",
                list_kind="bullet",
            ),
            action=SectionAction.TRANSLATE,
        ),
    ]
    doc = build_structured_document(classified)
    assert doc.title == "My Book"
    assert doc.authors == ["Jane Doe"]
    assert len(doc.content) == 3
    assert doc.content[0].type == "heading"
    assert doc.content[0].text == "Chapter One"
    assert doc.content[1].type == "paragraph"
    assert doc.content[2].type == "list"
    assert doc.content[2].ordered is False
    assert doc.content[2].items == ["one", "two"]


def test_omit_blocks_excluded():
    classified = [
        ClassifiedBlock(
            block=ContentBlock(type=BlockType.PARAGRAPH, text="gone"),
            action=SectionAction.OMIT,
        ),
        ClassifiedBlock(
            block=ContentBlock(type=BlockType.PARAGRAPH, text="kept"),
            action=SectionAction.TRANSLATE,
        ),
    ]
    doc = build_structured_document(classified)
    assert len(doc.content) == 1
    assert doc.content[0].text == "kept"


def test_skip_toc_blocks_excluded_from_structured_content():
    """SKIP (printed TOC) does not appear in StructuredDocument — avoids duplicate with generated TOC."""
    classified = [
        ClassifiedBlock(
            block=ContentBlock(
                type=BlockType.PARAGRAPH,
                text="Contents line",
                structural_tag=StructuralTag.TOC,
            ),
            action=SectionAction.SKIP,
        ),
    ]
    doc = build_structured_document(classified)
    assert len(doc.content) == 0


def test_paragraph_collapses_hard_line_breaks_inside_stanza():
    classified = [
        ClassifiedBlock(
            block=ContentBlock(
                type=BlockType.PARAGRAPH,
                text=(
                    "Paanch\n"
                    "hazaar theatres desh bhar mein meri kahani ko dikhane wale the "
                    "jo kabhi bhi mere dimaag mein\n"
                    "aayi thi."
                ),
            ),
            action=SectionAction.TRANSLATE,
        ),
    ]
    doc = build_structured_document(classified)
    assert doc.content[0].type == "paragraph"
    assert "\n" not in doc.content[0].text
    assert "Paanch hazaar theatres" in doc.content[0].text
    assert "dimaag mein aayi thi." in doc.content[0].text


def test_paragraph_blank_line_keeps_two_stanzas_in_one_block():
    classified = [
        ClassifiedBlock(
            block=ContentBlock(
                type=BlockType.PARAGRAPH,
                text="First stanza line one\nline two.\n\nSecond stanza here.",
            ),
            action=SectionAction.TRANSLATE,
        ),
    ]
    doc = build_structured_document(classified)
    assert doc.content[0].text == "First stanza line one line two.\n\nSecond stanza here."
