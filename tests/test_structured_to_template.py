"""Structured → template API model bridge."""

from app.models.structured_document import (
    StructuredDocument,
    StructuredHeading,
    StructuredList,
    StructuredParagraph,
    StructuredTable,
)
from app.services.structured_to_template import structured_to_document_for_template


def test_structured_to_template_blocks_table_as_paragraphs():
    doc = StructuredDocument(
        title="T",
        content=[
            StructuredHeading(level=1, text="H"),
            StructuredParagraph(text="Body"),
            StructuredList(ordered=True, items=["1", "2"]),
            StructuredTable(rows=[["a", "b"], ["c", "d"]]),
        ],
    )
    m = structured_to_document_for_template(doc)
    assert m.title == "T"
    assert m.blocks is not None
    assert "a | b" in m.model_dump_json()


def test_structured_to_template_level2_only_headings_are_chapter_starts():
    """Outline depth 2 (→ h3 in HTML) must still get chapter_start for PDF/DOCX breaks."""
    doc = StructuredDocument(
        title="T",
        content=[
            StructuredHeading(level=2, text="Chapter A"),
            StructuredParagraph(text="x"),
            StructuredHeading(level=2, text="Chapter B"),
        ],
    )
    m = structured_to_document_for_template(doc)
    heads = [b for b in (m.blocks or []) if getattr(b, "type", None) == "heading"]
    assert len(heads) == 2
    assert all(getattr(b, "chapter_start", False) for b in heads)


def test_structured_to_template_part_and_chapter_chapter_start_subsection_not():
    doc = StructuredDocument(
        title="T",
        content=[
            StructuredHeading(level=1, text="Part I"),
            StructuredHeading(level=2, text="Chapter 1"),
            StructuredHeading(level=3, text="Section 1.1"),
        ],
    )
    m = structured_to_document_for_template(doc)
    heads = [b for b in (m.blocks or []) if getattr(b, "type", None) == "heading"]
    assert [b.chapter_start for b in heads] == [True, True, False]
