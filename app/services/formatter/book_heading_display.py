"""Display normalization for common book milestone headings (PDF / HTML export)."""

from __future__ import annotations

import re

_PREFIX_PREFACE = re.compile(r"^\s*preface\b", re.I)
_PREFIX_INTRO = re.compile(r"^\s*introduction\b", re.I)
_PREFIX_ACK = re.compile(r"^\s*acknowledg(e)?ments?\b", re.I)
_PREFIX_CHAPTER_OR_PART = re.compile(r"^\s*(chapter|part)\b", re.I)


def format_book_main_heading_display(text: str | None) -> str:
    """Force canonical keyword casing for front-matter / part labels; keep the rest of the line.

    Examples: ``preface`` → ``Preface``; ``CHAPTER 1: open`` → ``Chapter 1: open``.
    """
    if not text:
        return ""
    t = " ".join(text.split()).strip()
    if not t:
        return t

    def _with_canonical_prefix(match: re.Match[str], canonical: str) -> str:
        rest = t[match.end() :].lstrip()
        if not rest:
            return canonical
        return f"{canonical} {rest}"

    m = _PREFIX_PREFACE.match(t)
    if m:
        return _with_canonical_prefix(m, "Preface")
    m = _PREFIX_INTRO.match(t)
    if m:
        return _with_canonical_prefix(m, "Introduction")
    m = _PREFIX_ACK.match(t)
    if m:
        return _with_canonical_prefix(m, "Acknowledgments")
    m = _PREFIX_CHAPTER_OR_PART.match(t)
    if m:
        word = (m.group(1) or "").lower()
        canonical = "Chapter" if word == "chapter" else "Part"
        return _with_canonical_prefix(m, canonical)
    return t


def is_book_milestone_heading_label(text: str | None) -> bool:
    """True if the line opens with a normalizable book milestone keyword."""
    if not text:
        return False
    t = " ".join(text.split()).strip()
    if not t:
        return False
    return bool(
        _PREFIX_PREFACE.match(t)
        or _PREFIX_INTRO.match(t)
        or _PREFIX_ACK.match(t)
        or _PREFIX_CHAPTER_OR_PART.match(t)
    )
