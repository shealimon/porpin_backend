"""Part splash (mini-TOC) pages → classify as OMIT."""

from app.models.document_models import BlockType, ClassifiedBlock, ContentBlock, SectionAction
from app.services.classifier.section_classifier import classify_blocks


def test_part_inventory_page_is_omitted():
    blocks = [
        ContentBlock(type=BlockType.HEADING, text="1", level=1),
        ContentBlock(
            type=BlockType.HEADING,
            text="Part 1. The Enemies of Clear Thinking",
            level=1,
        ),
        ContentBlock(
            type=BlockType.LIST,
            text=(
                "1. 1.1 Bura Sochna\n"
                "2. 1.2 Emotion Default\n"
                "3. 1.3 Ego Default\n"
                "4. 1.4 Social Default\n"
                "5. 1.5 Inertia Default\n"
                "6. 1.6 Saaf Soch Ki Taraf Default"
            ),
            list_kind="ordered",
        ),
        ContentBlock(type=BlockType.HEADING, text="Preface", level=2),
        ContentBlock(
            type=BlockType.PARAGRAPH,
            text="Maine August 2001 mein kaam shuru kiya.",
        ),
    ]
    out = classify_blocks(blocks)
    assert out[0].action == SectionAction.OMIT
    assert out[1].action == SectionAction.OMIT
    assert out[2].action == SectionAction.OMIT
    assert out[3].action == SectionAction.TRANSLATE
    assert out[4].action == SectionAction.TRANSLATE


def test_two_subsections_only_not_inventory():
    blocks = [
        ContentBlock(
            type=BlockType.HEADING,
            text="Part 9. Short",
            level=1,
        ),
        ContentBlock(
            type=BlockType.PARAGRAPH,
            text="9.1 One\n9.2 Two",
        ),
        ContentBlock(type=BlockType.PARAGRAPH, text="Real body starts."),
    ]
    out = classify_blocks(blocks)
    assert all(cb.action == SectionAction.TRANSLATE for cb in out)
