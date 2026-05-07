"""Book milestone heading display normalization."""

from app.services.formatter.book_heading_display import (
    format_book_main_heading_display,
    is_book_milestone_heading_label,
)


def test_format_main_headings_title_case_keyword():
    assert format_book_main_heading_display("preface") == "Preface"
    assert format_book_main_heading_display("INTRODUCTION") == "Introduction"
    assert format_book_main_heading_display("acknowledgements") == "Acknowledgments"
    assert format_book_main_heading_display("CHAPTER 1: start") == "Chapter 1: start"
    assert format_book_main_heading_display("part II — title") == "Part II — title"


def test_milestone_label_detection():
    assert is_book_milestone_heading_label("Preface")
    assert is_book_milestone_heading_label("notes on nothing") is False
