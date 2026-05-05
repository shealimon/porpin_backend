"""PDF running-header fragments (often missed by short-fragment dedupe)."""

from app.services.parser.pdf_running_header import (
    looks_like_pdf_running_header_line,
    strip_leading_pdf_navigation_crumbs,
)


def test_running_header_contents_pipe_pattern():
    s = (
        "Contents Preface Introduction: Aam Lamhon Mein Saaf Soch Ka Power | "
        "Thinking Badly—or Not Thinking at All?"
    )
    assert looks_like_pdf_running_header_line(s)


def test_running_header_contents_preface_intro_no_pipe():
    """TOC-page bleed shown above centered Contents title (no pipe)."""
    assert looks_like_pdf_running_header_line(
        "Contents Preface Introduction: Aam Lamhon Mein Saaf Soch Ka Power"
    )


def test_not_running_header_contents_of_prose():
    assert not looks_like_pdf_running_header_line(
        "Contents of the previous chapter were unclear."
    )


def test_running_header_chapter_part_pipe():
    assert looks_like_pdf_running_header_line("Chapter 3 | Habits Matter")
    assert looks_like_pdf_running_header_line("PART II | New Rules")


def test_not_running_header_pipe_in_dialogue():
    assert not looks_like_pdf_running_header_line("a | b")


def test_strip_navigation_crumbs_notes_index_preface_hinglish():
    s = "Notes Index Preface Maine August 2001 mein ek intelligence agency mein kaam shuru kiya."
    assert strip_leading_pdf_navigation_crumbs(s) == (
        "Maine August 2001 mein ek intelligence agency mein kaam shuru kiya."
    )


def test_strip_navigation_crumbs_two_tokens_guarded_by_to():
    assert (
        strip_leading_pdf_navigation_crumbs("Notes Index to the reader about margin lines.")
        == "Notes Index to the reader about margin lines."
    )


def test_strip_navigation_crumbs_single_index_preserved():
    assert (
        strip_leading_pdf_navigation_crumbs("Index of figures appears in the appendix.")
        == "Index of figures appears in the appendix."
    )


def test_strip_navigation_crumbs_requires_two_or_more_labels():
    assert strip_leading_pdf_navigation_crumbs("Preface Maine story") == "Preface Maine story"
