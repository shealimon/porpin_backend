"""Section OMIT / TRANSLATE rules."""

from app.models.document_models import BlockType, ContentBlock, SectionAction
from app.services.classifier.section_classifier import classify_blocks


def test_notes_index_copyright_regions_are_omit():
    blocks = [
        ContentBlock(type=BlockType.HEADING, text="Notes", level=2),
        ContentBlock(type=BlockType.PARAGRAPH, text="Should not appear."),
        ContentBlock(type=BlockType.HEADING, text="Body Again", level=2),
    ]
    out = classify_blocks(blocks)
    assert out[0].action == SectionAction.OMIT
    assert out[1].action == SectionAction.OMIT
    assert out[2].action == SectionAction.TRANSLATE


def test_acknowledgments_is_translate_not_omit():
    blocks = [
        ContentBlock(type=BlockType.HEADING, text="Acknowledgments", level=2),
        ContentBlock(type=BlockType.PARAGRAPH, text="Thanks to everyone."),
    ]
    out = classify_blocks(blocks)
    assert all(x.action == SectionAction.TRANSLATE for x in out)


def test_index_heading_omits_following_until_next_heading():
    blocks = [
        ContentBlock(type=BlockType.HEADING, text="Index", level=2),
        ContentBlock(type=BlockType.PARAGRAPH, text="alpha … 1"),
        ContentBlock(type=BlockType.HEADING, text="Epilogue", level=1),
        ContentBlock(type=BlockType.PARAGRAPH, text="Visible again."),
    ]
    out = classify_blocks(blocks)
    assert out[0].action == SectionAction.OMIT
    assert out[1].action == SectionAction.OMIT
    assert out[2].action == SectionAction.TRANSLATE
    assert out[3].action == SectionAction.TRANSLATE


def test_copyrights_heading_omits_section():
    blocks = [
        ContentBlock(type=BlockType.HEADING, text="Copyrights", level=2),
        ContentBlock(type=BlockType.PARAGRAPH, text="All rights reserved."),
    ]
    out = classify_blocks(blocks)
    assert out[0].action == SectionAction.OMIT
    assert out[1].action == SectionAction.OMIT
