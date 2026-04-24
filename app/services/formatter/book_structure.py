"""Detect book front-matter regions (title, author, TOC) for export layout; TOC is copied as-is (not translated)."""

from __future__ import annotations

import re

from app.models.document_models import BlockType, ContentBlock, StructuralTag
from app.utils.translate_filter import count_words


def _normalized(text: str | None) -> str:
    if not text:
        return ""
    return " ".join(text.lower().split())


def _toc_heading_triggers(text: str | None) -> bool:
    """TOC section headings (narrower than full index/skip lists)."""
    n = _normalized(text)
    if not n:
        return False
    if n in ("table of contents", "contents", "table of content", "contents page"):
        return True
    if n.startswith("table of contents") or n.startswith("table of content"):
        return True
    return False


def _looks_like_chapter_or_milestone_heading(text: str | None) -> bool:
    """First H1 that is clearly a chapter/part — not the book cover title."""
    if not text:
        return False
    t = text.strip()
    if len(t) > 200:
        return False
    if re.match(
        r"(?i)^(chapter|part|section|book)\s+[\w\d\"'\u2018\u2019\u201c\u201d,.;:\-—]+",
        t,
    ):
        return True
    if re.match(r"^\d+[\.\)]\s+\S", t):
        return True
    if re.match(r"(?i)^(prologue|epilogue|foreword|preface|introduction)\b", t):
        return True
    if re.match(r"^[IVXLC]+\.[\s\u00a0]+\S", t):
        return True
    return False


def _toc_standalone_line(text: str | None) -> bool:
    """Short standalone line that is only a TOC label."""
    if not text:
        return False
    t = text.strip()
    if len(t) > 100:
        return False
    return _toc_heading_triggers(t)


def apply_book_structure_tags(blocks: list[ContentBlock]) -> None:
    """
    Mutate blocks in place: set ``structural_tag`` for title page and TOC regions.

    TOC detection follows common book patterns (heading or standalone label, then
    entries until a chapter-style heading or a long body paragraph).
    """
    n = len(blocks)
    if not n:
        return

    in_toc = False
    toc_close_level = 1

    for i in range(n):
        block = blocks[i]

        if in_toc and block.type == BlockType.HEADING and block.text:
            if block.level <= toc_close_level and not _toc_heading_triggers(block.text):
                in_toc = False

        if not in_toc and block.type == BlockType.HEADING and block.text:
            if _toc_heading_triggers(block.text):
                in_toc = True
                toc_close_level = block.level
                blocks[i] = block.model_copy(update={"structural_tag": StructuralTag.TOC})
                continue

        if not in_toc and block.type in (BlockType.PARAGRAPH, BlockType.LIST) and block.text:
            t = block.text.strip()
            if (
                len(t) < 120
                and _toc_standalone_line(t)
                and not re.search(r"[.!?]\s+[A-Z]", t)
            ):
                in_toc = True
                toc_close_level = 1
                blocks[i] = block.model_copy(update={"structural_tag": StructuralTag.TOC})
                continue

        if in_toc:
            if block.type in (BlockType.PARAGRAPH, BlockType.LIST) and block.text:
                t = block.text.strip()
                if count_words(t) >= 40 or len(t) >= 320:
                    in_toc = False
                else:
                    blocks[i] = block.model_copy(update={"structural_tag": StructuralTag.TOC})
                    continue
            elif block.type == BlockType.TABLE:
                blocks[i] = block.model_copy(update={"structural_tag": StructuralTag.TOC})
                continue
            else:
                blocks[i] = block.model_copy(update={"structural_tag": StructuralTag.TOC})
                continue

    first_toc = next(
        (i for i, b in enumerate(blocks) if b.structural_tag == StructuralTag.TOC),
        n,
    )

    start = 0
    while start < first_toc and blocks[start].structural_tag == StructuralTag.TOC:
        start += 1

    if start >= first_toc:
        return

    b0 = blocks[start]
    if b0.type == BlockType.HEADING and b0.level == 1:
        if not _looks_like_chapter_or_milestone_heading(b0.text):
            blocks[start] = b0.model_copy(update={"structural_tag": StructuralTag.TITLE})
            start += 1
    elif b0.type in (BlockType.PARAGRAPH, BlockType.LIST) and count_words(b0.text) <= 15:
        blocks[start] = b0.model_copy(update={"structural_tag": StructuralTag.TITLE})
        start += 1

    author_lines = 0
    while start < first_toc and author_lines < 4:
        b = blocks[start]
        if b.structural_tag == StructuralTag.TOC:
            break
        if b.type == BlockType.HEADING:
            break
        if b.type in (BlockType.PARAGRAPH, BlockType.LIST) and b.text:
            if count_words(b.text) <= 25:
                blocks[start] = b.model_copy(update={"structural_tag": StructuralTag.AUTHOR})
                author_lines += 1
                start += 1
                continue
        break
