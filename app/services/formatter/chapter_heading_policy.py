"""Which outline levels get chapter page breaks and centered title styling (PDF + DOCX)."""

from __future__ import annotations

import re


def chapter_like_heading_text(text: str | None) -> bool:
    """True when the heading clearly marks a part / chapter / numbered book section.

    Used so titles like *PART ONE* still open as chapters even when outline-level
    heuristics pick a different ``chapter_lvl`` for narrative headings.
    """
    if not text:
        return False
    t = " ".join(text.split()).strip()
    if len(t) > 220:
        return False
    low = t.lower()
    if re.match(r"^(chapter|part|appendix|annex)\b", low):
        return True
    # PDFs sometimes split a chapter opener across lines:
    #   HEADING: "1"
    #   HEADING: "The Beginning"
    # Treat short numeric-only headings as chapter-like so downstream rendering can
    # merge the following title line into the chapter opener.
    if re.match(r"^\d{1,3}$", t):
        return True
    # Roman-numeral chapters: "CHAPTER IV", "Chapter XII".
    if re.match(r"^chapter\s+[ivxlcdm]{1,8}\b", low):
        return True
    # Named arcs / didactic sections (books + some coaching material).
    if re.match(
        r"^(law|narrative|lesson|unit|lecture|episode|module|segment)\s+(\d+|[a-z]+)\b",
        low,
    ):
        return True
    # Books, acts, numbered sections (avoid mid-sentence "Section …" via start anchor + short prefix).
    if re.match(r"^book\s+([ivxlcdm]{1,8}|\d{1,3})\b", low):
        return True
    if re.match(
        r"^section\s+\d{1,3}\b(?!\.\d)(?!\s+(?:of|in|on|for|that|which|the|a|an)\s)",
        low,
    ):
        return True
    if re.match(r"^act\s+[ivxlcdm]{1,8}\b", low):
        return True
    if re.match(r"^principle\s+\d{1,3}\b", low):
        return True
    # Interior numbering without the word "Chapter" (e.g. "1.2 The Emotion Default").
    if re.match(r"^\d{1,2}\.\d{1,3}\s+\S", t):
        return True
    return False


def chapter_start_level(levels: list[int]) -> int | None:
    """Pick which outline level should be treated as a new-chapter boundary.

    Heuristic:
    - Usually the *shallowest* heading level is the chapter boundary.
    - If there's only a single level-1 heading (often a book/part title) and level-2
      headings exist, treat level-2 as the real chapters. This prevents subheadings
      from getting chapter-number pages and forced page breaks.

    Headings whose text matches :func:`chapter_like_heading_text` can still use chapter
    layout when this level doesn't match—see :func:`is_chapter_outline_level`.
    """
    if not levels:
        return None
    min_lvl = min(levels)
    if min_lvl != 1:
        return min_lvl
    count1 = sum(1 for x in levels if x == 1)
    if count1 <= 1 and any(x == 2 for x in levels):
        return 2
    return 1


def is_chapter_outline_level(
    level: int,
    chapter_lvl: int | None,
    *,
    heading_text: str | None = None,
) -> bool:
    """True when this heading should use centered chapter-open styling (+ page break)."""
    if chapter_like_heading_text(heading_text):
        return True
    if chapter_lvl is None:
        return False
    return level == chapter_lvl
