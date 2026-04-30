"""Which outline levels get chapter page breaks and centered title styling (PDF + DOCX)."""

from __future__ import annotations


def chapter_start_level(levels: list[int]) -> int | None:
    """Pick which outline level should be treated as a new-chapter boundary.

    Heuristic:
    - Usually the *shallowest* heading level is the chapter boundary.
    - If there's only a single level-1 heading (often a book/part title) and level-2
      headings exist, treat level-2 as the real chapters. This prevents subheadings
      from getting chapter-number pages and forced page breaks.
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


def is_chapter_outline_level(level: int, chapter_lvl: int | None) -> bool:
    """True iff this heading level starts a new chapter."""
    if chapter_lvl is None:
        return False
    return level == chapter_lvl
