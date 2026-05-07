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


def test_structured_to_template_formats_book_milestone_headings():
    doc = StructuredDocument(
        title="T",
        content=[
            StructuredHeading(level=2, text="preface", kind="chapter"),
            StructuredParagraph(text="Text."),
        ],
    )
    m = structured_to_document_for_template(doc)
    h = next(b for b in (m.blocks or []) if getattr(b, "type", None) == "heading")
    assert h.text == "Preface"
    assert h.chapter_start is True


def test_structured_to_template_formats_chapter_keyword():
    doc = StructuredDocument(
        title="T",
        content=[
            StructuredHeading(level=2, text="CHAPTER 1: Open", kind="chapter"),
            StructuredParagraph(text="x"),
        ],
    )
    m = structured_to_document_for_template(doc)
    h = next(b for b in (m.blocks or []) if getattr(b, "type", None) == "heading")
    assert h.text.startswith("Chapter ")
    assert h.chapter_start is True


def test_semantic_section_heading_not_promoted_to_chapter_by_outline_level():
    """Explicit ``kind='heading'`` must not become a chapter opener only because level==chapter_lvl."""
    doc = StructuredDocument(
        title="T",
        content=[
            StructuredHeading(level=1, text="Part I", kind="chapter"),
            StructuredParagraph(text="x"),
            StructuredHeading(level=2, text="Introduction", kind="heading"),
            StructuredHeading(level=3, text="Scope", kind="subheading"),
        ],
    )
    m = structured_to_document_for_template(doc)
    heads = [b for b in (m.blocks or []) if getattr(b, "type", None) == "heading"]
    assert len(heads) == 3
    assert heads[0].chapter_start is True
    assert heads[1].chapter_start is False
    assert heads[1].is_subheading is False
    assert heads[2].is_subheading is True


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


def test_structured_to_template_drops_merged_toc_spill_paragraphs():
    doc = StructuredDocument(
        title="Book",
        content=[
            StructuredParagraph(
                text=(
                    "Cover Title Page Dedication Preface Author Note "
                    "Chapter 1: Title Only Here"
                )
            ),
            StructuredParagraph(text="Real narrative body starts here."),
        ],
    )
    m = structured_to_document_for_template(doc)
    texts = [b.text for b in (m.blocks or []) if hasattr(b, "text") and b.text]
    assert "Cover Title Page" not in " ".join(texts)
    assert any("Real narrative body" in x for x in texts)


def test_structured_to_template_drops_printed_toc_leader_lines():
    doc = StructuredDocument(
        title="Book",
        content=[
            StructuredHeading(
                level=2,
                text="CHAPTER 1.5 ................................ 35",
            ),
            StructuredParagraph(text="The Inertia Default ........................ 35"),
            StructuredList(
                ordered=True,
                items=[
                    "PART 1 ................................... 14",
                    "PART 2 ................................... 18",
                ],
            ),
            StructuredParagraph(text="Actual chapter body begins here with enough text."),
        ],
    )
    m = structured_to_document_for_template(doc)
    dumped = m.model_dump_json()
    assert "CHAPTER 1.5" not in dumped
    assert "Inertia Default" not in dumped
    assert "PART 1" not in dumped
    assert "Actual chapter body" in dumped


def test_structured_to_template_skips_redundant_part_subsection_inventory_list():
    """Part splash pages repeat 1.1 … 1.n in a list; Contents already covers these."""
    items = [
        "1. 1.1 Bura Sochna—ya Bilkul Nahi Sochna?",
        "2. 1.2 Emotion Default",
        "3. 1.3 Ego Default",
        "4. 1.4 Social Default",
        "5. 1.5 Inertia Default",
        "6. 1.6 Saaf Soch Ki Taraf Default",
    ]
    doc = StructuredDocument(
        title="Doc",
        content=[
            StructuredHeading(level=1, text="1", kind="chapter"),
            StructuredHeading(
                level=1,
                text="Part 1. The Enemies of Clear Thinking",
                kind="chapter",
            ),
            StructuredList(ordered=True, items=items),
            StructuredParagraph(text="First real paragraph of the part goes here."),
        ],
    )
    m = structured_to_document_for_template(doc)
    texts: list[str] = []
    for b in m.blocks or []:
        if hasattr(b, "items"):
            texts.extend(list(getattr(b, "items", []) or []))
        if hasattr(b, "text") and getattr(b, "text", None):
            texts.append(str(b.text))
    blob = " ".join(texts)
    assert "1.1 Bura" not in blob
    assert "Emotion Default" not in blob
    assert "First real paragraph" in blob
    heads = [b for b in (m.blocks or []) if getattr(b, "type", None) == "heading"]
    assert any("Part 1" in (getattr(h, "text", "") or "") for h in heads)


def test_structured_to_template_part_inventory_after_lone_part_number_only():
    doc = StructuredDocument(
        title="D",
        content=[
            StructuredHeading(level=1, text="2", kind="chapter"),
            StructuredList(
                ordered=True,
                items=[
                    "1. 2.1 Alpha",
                    "2. 2.2 Beta",
                    "3. 2.3 Gamma",
                ],
            ),
            StructuredParagraph(text="Body next."),
        ],
    )
    m = structured_to_document_for_template(doc)
    dumped = m.model_dump_json()
    assert "2.1 Alpha" not in dumped
    assert "Body next" in dumped


def test_structured_to_template_omits_printed_toc_heading_then_keeps_real_part():
    doc = StructuredDocument(
        title="T",
        content=[
            StructuredHeading(
                level=2,
                text="Part 1. The Enemies of Clear Thinking 4",
                kind="chapter",
            ),
            StructuredHeading(
                level=2,
                text="Part 1. The Enemies of Clear Thinking",
                kind="chapter",
            ),
            StructuredParagraph(text="Opening paragraph."),
        ],
    )
    m = structured_to_document_for_template(doc)
    heads = [b for b in (m.blocks or []) if getattr(b, "type", None) == "heading"]
    assert len(heads) == 1
    assert not (heads[0].text or "").rstrip().endswith("4")
    assert "Opening paragraph" in m.model_dump_json()


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


def test_structured_to_template_preserves_one_block_per_paragraph_for_pdf_docx():
    """Each StructuredParagraph stays one output block — matches source paragraph boundaries."""
    doc = StructuredDocument(
        title="T",
        content=[
            StructuredParagraph(text="First."),
            StructuredParagraph(text="Second."),
            StructuredParagraph(text="Third."),
        ],
    )
    m = structured_to_document_for_template(doc)
    paras = [b for b in (m.blocks or []) if getattr(b, "type", None) == "paragraph"]
    assert len(paras) == 3


def test_structured_to_template_preserves_paragraph_boundaries_before_heading():
    doc = StructuredDocument(
        title="T",
        content=[
            StructuredParagraph(text="One. Two."),
            StructuredHeading(level=2, text="Mid"),
            StructuredParagraph(text="Three."),
        ],
    )
    m = structured_to_document_for_template(doc)
    types = [getattr(b, "type", None) for b in (m.blocks or [])]
    assert types == ["paragraph", "heading", "paragraph"]


def test_decimal_outline_paragraph_becomes_ordered_list():
    doc = StructuredDocument(
        title="T",
        content=[
            StructuredParagraph(
                text="1.1 First subsection title 1.2 Second title 1.3 Third title here"
            ),
        ],
    )
    m = structured_to_document_for_template(doc)
    assert len(m.blocks) == 1
    blk = m.blocks[0]
    assert blk.type == "list"
    assert blk.ordered is True
    assert len(blk.items) == 3
    assert blk.items[0].startswith("1.1 ")


def test_finalize_toc_dedupes_and_drops_prose():
    from app.services.structured_to_template import _finalize_toc_entries

    long_prose = (
        "This is narrative body that should never be a Contents line. "
        "It goes on with enough words to pass filters? Then it continues. "
        "Even more words here for the word count threshold in the detector."
    )
    toc = [
        {"text": "Part 1. The Enemies", "anchor": "a1", "level": 2},
        {"text": "Part 1. The Enemies", "anchor": "a2", "level": 2},
        {"text": long_prose, "anchor": "a3", "level": 3},
    ]
    out = _finalize_toc_entries(toc)
    assert len(out) == 1
    assert out[0]["anchor"] == "a1"
