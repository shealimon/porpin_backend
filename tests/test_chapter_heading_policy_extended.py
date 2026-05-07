"""Extended chapter-opener patterns (book / act / section / principle)."""

from app.services.formatter.chapter_heading_policy import chapter_like_heading_text


def test_book_roman_and_arabic():
    assert chapter_like_heading_text("BOOK I")
    assert chapter_like_heading_text("Book 2 — The Return")


def test_act_roman_numeral():
    assert chapter_like_heading_text("Act III")


def test_section_number_heading():
    assert chapter_like_heading_text("Section 1 — Overview")


def test_section_decimal_subsection_not_major_chapter():
    assert chapter_like_heading_text("Section 1.1 — Subsection A") is False


def test_section_clause_not_chapter_like():
    assert chapter_like_heading_text("Section 1 of the tax code explains") is False


def test_principle_numbered():
    assert chapter_like_heading_text("Principle 4")
