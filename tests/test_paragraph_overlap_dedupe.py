"""Tests for post-translate consecutive paragraph deduplication."""

from __future__ import annotations

from app.models.document_models import (
    BlockType,
    ClassifiedBlock,
    ContentBlock,
    SectionAction,
    StructuralTag,
)
from app.services.document_pipeline.paragraph_overlap_dedupe import (
    dedupe_consecutive_redundant_translate_paragraphs,
)


def _p(text: str, *, tag: StructuralTag | None = None) -> ClassifiedBlock:
    return ClassifiedBlock(
        block=ContentBlock(type=BlockType.PARAGRAPH, text=text, structural_tag=tag),
        action=SectionAction.TRANSLATE,
    )


def test_dedupe_truncated_quote_hinglish_pair_drops_shorter() -> None:
    prev = (
        'Ye aise hai jaise hum apne dimaag ki andar ki awaaz se ye expect karte hain, "STOP! YE EK'
    )
    nxt = (
        'Yeh aisa hai jaise hum expect karte hain ki hamare dimaag ki andar ki awaaz kahe, '
        '"STOP! YEH EK MOMENT HAI JAB AAPKO SOCHNA HAI!"'
    )
    out = dedupe_consecutive_redundant_translate_paragraphs([_p(prev), _p(nxt)])
    assert len(out) == 1
    assert out[0].block.text == nxt


def test_dedupe_does_not_touch_structural_tag_blocks_as_neighbors() -> None:
    """TOC/title paragraphs are not candidates; body pairs after them still dedupe."""
    prev = (
        'Ye aise hai jaise hum apne dimaag ki andar ki awaaz se ye expect karte hain, "STOP! YE EK'
    )
    nxt = (
        'Yeh aisa hai jaise hum expect karte hain ki hamare dimaag ki andar ki awaaz kahe, '
        '"STOP! YEH EK MOMENT HAI JAB AAPKO SOCHNA HAI!"'
    )
    toc = _p("some toc line", tag=StructuralTag.TOC)
    body_prev = _p(prev)
    body_nxt = _p(nxt)
    out = dedupe_consecutive_redundant_translate_paragraphs([toc, body_prev, body_nxt])
    assert len(out) == 2
    assert out[0].block.structural_tag == StructuralTag.TOC
    assert out[1].block.text == nxt


def test_dedupe_does_not_merge_related_mind_brain_sentences() -> None:
    a = (
        "The mind often wanders when we are tired and need to make complex decisions "
        "quickly without any support from others in our daily tasks."
    )
    b = (
        "The brain also struggles severely when we are tired and need to make complex "
        "decisions without support from others in daily life situations that demand clarity."
    )
    out = dedupe_consecutive_redundant_translate_paragraphs([_p(a), _p(b)])
    assert len(out) == 2


def test_dedupe_prefix_continuation_without_sentence_end() -> None:
    first = "This is an incomplete line that keeps going and has no final period mark"
    second = first + " so we add more words here to finish the thought properly now."
    out = dedupe_consecutive_redundant_translate_paragraphs([_p(first), _p(second)])
    assert len(out) == 1
    assert out[0].block.text == second


def test_dedupe_no_prefix_drop_when_first_sentence_complete() -> None:
    first = "Chapter one begins."
    second = "Chapter one begins with a story about travel and maps and lessons learned."
    out = dedupe_consecutive_redundant_translate_paragraphs([_p(first), _p(second)])
    assert len(out) == 2


def test_dedupe_interior_glued_hinglish_duplicate_in_one_paragraph() -> None:
    """PDF merge can put truncated + full metaphor in a single block."""
    prev = (
        'Ye aise hai jaise hum apne dimaag ki andar ki awaaz se ye expect karte hain, "STOP! YE EK'
    )
    nxt = (
        'Yeh aisa hai jaise hum expect karte hain ki hamare dimaag ki andar ki awaaz kahe, '
        '"STOP! YEH EK MOMENT HAI JAB AAPKO SOCHNA HAI!"'
    )
    glued = f"Lead text. {prev} {nxt} Aur yahan aage chalta hai."
    out = dedupe_consecutive_redundant_translate_paragraphs([_p(glued)])
    assert len(out) == 1
    assert prev not in (out[0].block.text or "")
    assert nxt in (out[0].block.text or "")
    assert "Aur yahan aage chalta hai." in (out[0].block.text or "")


def test_strip_inner_voice_smart_quotes_or_esi_metaphor() -> None:
    prev = (
        "Ye aise hai jaise hum apne dimaag ki andar ki awaaz se ye expect karte hain, "
        "\u201cSTOP! YE EK"
    )
    nxt = (
        "Yeh esi hai jaise hum expect karte hain ki hamare dimaag ki andar ki awaaz kahe, "
        "\u201cSTOP! YEH EK MOMENT HAI!\u201d"
    )
    glued = f"Preamble. {prev} {nxt}"
    out = dedupe_consecutive_redundant_translate_paragraphs([_p(glued)])
    assert "Ye aise hai jaise hum apne" not in (out[0].block.text or "")
    assert "Yeh esi hai jaise" in (out[0].block.text or "")


def test_dedupe_multiple_passes_collapse_truncation_chain() -> None:
    a = 'Part one of thought "OPEN'
    b = 'Part one of thought "OPEN QUOTE'
    c = (
        'Part one of thought "OPEN QUOTE TEXT HERE" and the rest of the idea continues '
        "with enough length to satisfy extension heuristics."
    )
    out = dedupe_consecutive_redundant_translate_paragraphs([_p(a), _p(b), _p(c)])
    assert len(out) == 1
    assert out[0].block.text == c
