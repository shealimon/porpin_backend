"""Semantic enrichment before translation (structure + metadata hints)."""

from app.models.document_models import (
    BlockType,
    ClassifiedBlock,
    ContentBlock,
    SectionAction,
    StructuralTag,
)
from app.services.document_pipeline.stages.semantic_enrichment import enrich_classified_blocks


def test_demote_long_heading_to_paragraph():
    classified = [
        ClassifiedBlock(
            block=ContentBlock(
                type=BlockType.HEADING,
                text="This is a long heading-like line that is actually prose from a bad PDF extract "
                "and should not survive as a heading because it carries too many words.",
                level=2,
            ),
            action=SectionAction.TRANSLATE,
        ),
    ]
    out, meta = enrich_classified_blocks(classified)
    assert out[0].block.type == BlockType.PARAGRAPH
    assert out[0].block.semantic_role == "paragraph"
    assert meta.document_type == "general"


def test_chapter_label_gets_semantic_chapter_and_level_1():
    classified = [
        ClassifiedBlock(
            block=ContentBlock(
                type=BlockType.HEADING,
                text="Chapter 3: The Experiment",
                level=2,
            ),
            action=SectionAction.TRANSLATE,
        ),
        ClassifiedBlock(
            block=ContentBlock(type=BlockType.PARAGRAPH, text="Body text."),
            action=SectionAction.TRANSLATE,
        ),
    ]
    out, meta = enrich_classified_blocks(classified)
    assert out[0].block.semantic_role == "chapter"
    assert out[0].block.level == 1
    assert "Chapter 3" in meta.detected_chapter_titles[0]


def test_consecutive_non_chapter_headings_second_is_subheading():
    classified = [
        ClassifiedBlock(
            block=ContentBlock(type=BlockType.HEADING, text="Introduction", level=2),
            action=SectionAction.TRANSLATE,
        ),
        ClassifiedBlock(
            block=ContentBlock(type=BlockType.HEADING, text="Scope", level=2),
            action=SectionAction.TRANSLATE,
        ),
    ]
    out, _ = enrich_classified_blocks(classified)
    assert out[0].block.semantic_role == "heading"
    assert out[1].block.semantic_role == "subheading"


def test_title_blocks_tagged_document_title():
    classified = [
        ClassifiedBlock(
            block=ContentBlock(
                type=BlockType.HEADING,
                text="My Novel",
                level=1,
                structural_tag=StructuralTag.TITLE,
            ),
            action=SectionAction.TRANSLATE,
        ),
    ]
    out, _ = enrich_classified_blocks(classified)
    assert out[0].block.semantic_role == "document_title"
