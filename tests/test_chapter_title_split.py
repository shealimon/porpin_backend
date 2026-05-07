"""Chapter title splitting for PDF/HTML context."""

from app.services.document_template_render.context import _split_chapter_title


def test_split_arabic_chapter():
    n, rest = _split_chapter_title("Chapter 12: The Gate")
    assert n == 12
    assert rest == "The Gate"


def test_split_roman_chapter_label():
    n, rest = _split_chapter_title("Chapter IV — Aftermath")
    assert n == "IV"
    assert rest == "Aftermath"
