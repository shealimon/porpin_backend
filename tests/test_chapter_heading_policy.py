"""Chapter open detection for DOCX/PDF export."""

from app.services.formatter.chapter_heading_policy import (
    chapter_like_heading_text,
    chapter_start_level,
    is_chapter_outline_level,
)


def test_chapter_like_patterns():
    assert chapter_like_heading_text("CHAPTER 1")
    assert chapter_like_heading_text("chapter twelve · foo")
    assert chapter_like_heading_text("PART TWO")
    assert chapter_like_heading_text("1")
    assert chapter_like_heading_text("1.2 The Emotion Default")
    assert not chapter_like_heading_text("Always An Entrepreneur")
    assert not chapter_like_heading_text("")


def test_is_chapter_outline_respects_explicit_chapter_words_when_level_differs():
    chapter_lvl = 2
    assert is_chapter_outline_level(
        1, chapter_lvl, heading_text="PART ONE"
    )
    assert not is_chapter_outline_level(1, chapter_lvl, heading_text="Randomaside")


def test_chapter_start_level_fallback_unchanged():
    assert chapter_start_level([2, 2, 3]) == 2
    assert chapter_start_level([1, 2, 2]) == 2
