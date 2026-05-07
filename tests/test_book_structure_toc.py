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


def test_merged_spine_toc_blob_tagged_as_toc():
    """One paragraph listing Cover… Chapter 1… Chapter 4… must not become translated body."""
    blob = (
        "Cover Title Page Dedication Preface: Before You Enter Author Note "
        "Chapter 1: Alpha Chapter 2: Beta Chapter 3: Gamma Chapter 4: Delta"
    )
    blocks = [
        ContentBlock(type=BlockType.HEADING, text="Contents", level=1),
        ContentBlock(type=BlockType.PARAGRAPH, text=blob),
        ContentBlock(
            type=BlockType.PARAGRAPH,
            text="Real preface body starts here with enough words to be clearly narrative.",
        ),
    ]
    apply_book_structure_tags(blocks)
    assert blocks[0].structural_tag == StructuralTag.TOC
    assert blocks[1].structural_tag == StructuralTag.TOC
    assert blocks[2].structural_tag is None


def test_merged_toc_blob_tagged_even_without_prior_contents_heading():
    blocks = [
        ContentBlock(
            type=BlockType.PARAGRAPH,
            text=(
                "Cover Title Page Dedication Preface Chapter 1: One Chapter 2: Two "
                "Chapter 3: Three Chapter 4: Four extra words to pass minimum length "
                "for the merged TOC detector so we exercise the post-pass sweep."
            ),
        ),
        ContentBlock(type=BlockType.HEADING, text="Preface", level=2),
    ]
    apply_book_structure_tags(blocks)
    assert blocks[0].structural_tag == StructuralTag.TOC
    assert blocks[1].structural_tag is None


def test_merged_short_cover_chapter1_spill_detected():
    from app.services.formatter.book_structure import looks_like_merged_toc_body_spill

    t = (
        "Cover Title Page Dedication Preface: Aapke Entry Se Pehle: Author ka Note "
        "Chapter 1: Aapka Phone Booth Moment Kya Hai?"
    )
    assert looks_like_merged_toc_body_spill(t)


def test_merged_long_chapter_list_and_ack_detected():
    from app.services.formatter.book_structure import looks_like_merged_toc_body_spill

    parts = [f"Chapter {i}: T{i}" for i in range(2, 12)]
    t = " ".join(parts) + " Acknowledgments Notes"
    assert looks_like_merged_toc_body_spill(t)


def test_toc_headings_chapter_rows_stay_tagged_until_body():
    """TOC rows extracted as HEADING (CHAPTER 1.5 …) must not end TOC and spill pages into body."""
    blocks = [
        ContentBlock(type=BlockType.HEADING, text="Contents", level=1),
        ContentBlock(
            type=BlockType.HEADING,
            text="CHAPTER 1.5 ................................ 35",
            level=2,
        ),
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


def test_orphan_printed_toc_run_tagged_without_contents_heading():
    blocks = [
        ContentBlock(
            type=BlockType.PARAGRAPH,
            text="PART 1 ......................................... 14",
        ),
        ContentBlock(
            type=BlockType.PARAGRAPH,
            text="CHAPTER 1.1  Thinking badly .................... 16",
        ),
        ContentBlock(
            type=BlockType.PARAGRAPH,
            text="CHAPTER 1.2  Emotion Default .................... 22",
        ),
        ContentBlock(
            type=BlockType.PARAGRAPH,
            text="Preface I started working at an agency in 2001 and the world changed.",
        ),
    ]
    apply_book_structure_tags(blocks)
    assert blocks[0].structural_tag == StructuralTag.TOC
    assert blocks[1].structural_tag == StructuralTag.TOC
    assert blocks[2].structural_tag == StructuralTag.TOC
    assert blocks[3].structural_tag is None


def test_multi_line_paragraph_opens_contents_region():
    blocks = [
        ContentBlock(
            type=BlockType.PARAGRAPH,
            text="Contents\nPART 1 ................................ 14",
        ),
        ContentBlock(
            type=BlockType.PARAGRAPH,
            text="Real narrative starts here and continues with enough words to pass.",
        ),
    ]
    apply_book_structure_tags(blocks)
    assert blocks[0].structural_tag == StructuralTag.TOC
    assert blocks[1].structural_tag is None


def test_merge_short_paragraphs_part_page_unmerged():
    from app.services.parser.pdf_parser import _merge_short_paragraphs

    pg = 2
    blocks = [
        ContentBlock(type=BlockType.PARAGRAPH, text="Part 1 The enemies", source_page=pg),
        ContentBlock(type=BlockType.PARAGRAPH, text="1.1 Thinking badly", source_page=pg),
    ]
    out = _merge_short_paragraphs(blocks, max_gap=0)
    assert len(out) == 2


def test_printed_toc_heading_noise_detects_toc_part_line_with_page():
    from app.services.formatter.book_structure import looks_like_printed_toc_heading_noise

    assert looks_like_printed_toc_heading_noise(
        "Part 1. The Enemies of Clear Thinking 4"
    )
    assert looks_like_printed_toc_heading_noise(
        "CHAPTER 1.1 ................................ 16"
    )
    assert looks_like_printed_toc_heading_noise(
        "CONCLUSION THE VALUE OF CLEAR THINKING 8"
    )
    assert not looks_like_printed_toc_heading_noise(
        "Part 1. The Enemies of Clear Thinking"
    )
    assert not looks_like_printed_toc_heading_noise(
        "Chapter 1: Opening in the real body without page column"
    )
    assert not looks_like_printed_toc_heading_noise("Chapter 1")
    assert not looks_like_printed_toc_heading_noise("Part I")


