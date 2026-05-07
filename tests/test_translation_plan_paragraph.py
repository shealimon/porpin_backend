"""Paragraph merge before translation planning + whole-paragraph GPT at ratio 1.0."""

from app.models.document_models import BlockType, ClassifiedBlock, ContentBlock, SectionAction, StructuralTag
from app.services.translation_plan import merge_adjacent_translate_paragraphs
from app.utils.translate_filter import plan_paragraph_for_translation


def test_plan_paragraph_full_api_when_ratio_one():
    plans = plan_paragraph_for_translation(
        "First sentence. Second sentence!",
        max_api_word_ratio=1.0,
    )
    assert len(plans) == 1
    assert plans[0].send_to_api
    assert "First sentence" in plans[0].text


def test_merge_joins_sentence_fragments_mid_flow():
    blocks = [
        ClassifiedBlock(
            block=ContentBlock(type=BlockType.PARAGRAPH, text="When you ask"),
            action=SectionAction.TRANSLATE,
        ),
        ClassifiedBlock(
            block=ContentBlock(type=BlockType.PARAGRAPH, text="people about improving"),
            action=SectionAction.TRANSLATE,
        ),
    ]
    out = merge_adjacent_translate_paragraphs(blocks)
    assert len(out) == 1
    assert out[0].block.text == "When you ask people about improving"


def test_merge_keeps_boundary_after_sentence_capital_followup():
    blocks = [
        ClassifiedBlock(
            block=ContentBlock(type=BlockType.PARAGRAPH, text="It was cold outside."),
            action=SectionAction.TRANSLATE,
        ),
        ClassifiedBlock(
            block=ContentBlock(type=BlockType.PARAGRAPH, text="He opened the door."),
            action=SectionAction.TRANSLATE,
        ),
    ]
    out = merge_adjacent_translate_paragraphs(blocks)
    assert len(out) == 2
    assert out[0].block.text == "It was cold outside."
    assert out[1].block.text == "He opened the door."


def test_merge_respects_period_lowercase_continuation_after_split():
    """Lowercase opener after punctuation still continues one logical paragraph shard."""
    blocks = [
        ClassifiedBlock(
            block=ContentBlock(type=BlockType.PARAGRAPH, text="She stopped."),
            action=SectionAction.TRANSLATE,
        ),
        ClassifiedBlock(
            block=ContentBlock(type=BlockType.PARAGRAPH, text="then kept walking."),
            action=SectionAction.TRANSLATE,
        ),
    ]
    out = merge_adjacent_translate_paragraphs(blocks)
    assert len(out) == 1
    assert "She stopped." in out[0].block.text
    assert "then kept walking" in out[0].block.text


def test_merge_keeps_preface_and_introduction_separate_from_body():
    blocks = [
        ClassifiedBlock(
            block=ContentBlock(type=BlockType.PARAGRAPH, text="Some preface body text here."),
            action=SectionAction.TRANSLATE,
        ),
        ClassifiedBlock(
            block=ContentBlock(type=BlockType.PARAGRAPH, text="Preface"),
            action=SectionAction.TRANSLATE,
        ),
        ClassifiedBlock(
            block=ContentBlock(type=BlockType.PARAGRAPH, text="Maine August 2001 mein kaam shuru kiya."),
            action=SectionAction.TRANSLATE,
        ),
        ClassifiedBlock(
            block=ContentBlock(type=BlockType.PARAGRAPH, text="Introduction"),
            action=SectionAction.TRANSLATE,
        ),
        ClassifiedBlock(
            block=ContentBlock(
                type=BlockType.PARAGRAPH,
                text="what happens in ordinary moments determines your future.",
            ),
            action=SectionAction.TRANSLATE,
        ),
    ]
    out = merge_adjacent_translate_paragraphs(blocks)
    parts = [cb.block.text.strip() for cb in out]
    assert "Preface" in parts
    assert "Introduction" in parts
    assert any("Some preface body" in p for p in parts)
    assert not any("Preface Maine" in p.replace("\n\n", " ") for p in parts)
    assert not any("Introduction what happens" in p.replace("\n\n", " ") for p in parts)
    body = ContentBlock(type=BlockType.PARAGRAPH, text="Runs on")
    title = ContentBlock(
        type=BlockType.PARAGRAPH,
        text="Book Title Only",
        structural_tag=StructuralTag.TITLE,
    )
    toc_row = ContentBlock(
        type=BlockType.PARAGRAPH,
        text="Preface ... vii",
        structural_tag=StructuralTag.TOC,
    )
    blocks = [
        ClassifiedBlock(block=title, action=SectionAction.TRANSLATE),
        ClassifiedBlock(block=toc_row, action=SectionAction.SKIP),
        ClassifiedBlock(block=body, action=SectionAction.TRANSLATE),
        ClassifiedBlock(
            block=ContentBlock(type=BlockType.PARAGRAPH, text="continuation"),
            action=SectionAction.TRANSLATE,
        ),
    ]
    out = merge_adjacent_translate_paragraphs(blocks)
    assert len(out) == 3
    assert out[0].block.structural_tag == StructuralTag.TITLE
    assert out[1].action == SectionAction.SKIP
    assert out[2].block.text == "Runs on continuation"

