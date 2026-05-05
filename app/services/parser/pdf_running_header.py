"""Detect PDF interior running heads / TOC bleed lines that must not become body blocks."""

from __future__ import annotations

import re

# End-matter / nav labels glued into the first word of body after a TOC spread (common in
# trade PDFs). Stripped only when two or more consecutive tokens match, to avoid chewing real prose.
_NAV_CRUMB_WORDS = frozenset(
    {
        "notes",
        "index",
        "preface",
        "foreword",
        "contents",
        "bibliography",
        "glossary",
        "illustrations",
        "abbreviations",
        "dedication",
        "acknowledgments",
        "acknowledgements",
        "appendix",
        "appendices",
    }
)
_FOLLOWS_TWO_TOKEN_NAV = frozenset(
    {"to", "of", "for", "and", "on", "in", "by", "about", "from", "with", "at"}
)
_MAX_LEADING_NAV_WORDS = 8


def _nav_token_key(word: str) -> str:
    return re.sub(r"^[^\w]+|[^\w]+$", "", word, flags=re.UNICODE).lower()


def strip_leading_pdf_navigation_crumbs(text: str | None) -> str:
    """Remove ``Notes Index Preface …`` style bleed at the start of a paragraph.

    Many books repeat small caps / sidebar nav labels near the spine; PyMuPDF puts them on the
    same text line as the first body sentence. Whole-line running-header drop does not run.
    """
    if not text:
        return ""
    raw = text.strip()
    parts = raw.split()
    if len(parts) < 3:
        return raw
    i = 0
    while (
        i < len(parts)
        and i < _MAX_LEADING_NAV_WORDS
        and _nav_token_key(parts[i]) in _NAV_CRUMB_WORDS
    ):
        i += 1
    if i < 2:
        return raw
    rest = parts[i:]
    if not rest:
        return raw
    # "Notes Index to the reader" — don't strip two labels before "to".
    if i == 2 and _nav_token_key(rest[0]) in _FOLLOWS_TWO_TOKEN_NAV:
        return raw
    return " ".join(rest)


def looks_like_pdf_running_header_line(text: str | None) -> bool:
    """Typical trade-book runners OCR'd into flow text (often skipped by short-fragment dedupe).

    Examples:
    - ``Contents … | Chapter title …``
    - ``Contents Preface Introduction: …`` (TOC spread header glued without a pipe)
    """
    if not text:
        return False
    t = " ".join(text.split()).strip()
    if len(t) < 10 or len(t) > 420:
        return False
    low = t.lower()
    if low.startswith("contents of ") and "|" not in t:
        return False

    if "|" in t:
        if low.startswith("contents ") or low.startswith("table of contents"):
            return True
        if re.match(r"(?i)^(chapter|part)\s+[\w\d.]+\s*\|", t):
            return True
        return False

    # No pipe: glued TOC labels / Contents-page runners (allow longer translated subtitles).
    if len(t) > 320:
        return False
    if re.match(r"(?is)^contents\s+(preface|introduction|foreword)\b", t):
        return True
    if re.match(r"(?is)^contents\s+preface\s+introduction\b", t):
        return True
    # "Contents Part 1 …" bleed on TOC recto pages
    if re.match(r"(?is)^contents\s+part\s+\d+\b", t):
        return True
    return False
