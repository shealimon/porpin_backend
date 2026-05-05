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


def _looks_like_title_imprint_or_series_boilerplate(text: str | None) -> bool:
    """Title page / copyright / series lines mistaken for TOC rows when bundled with Contents."""
    if not text:
        return False
    t = text.strip()
    if len(t) > 180:
        return False
    n = _normalized(text)
    needles = (
        "from the russian",
        "from the french",
        "from the german",
        "from the polish",
        "translated by",
        "translation by",
        "printed in great britain",
        "printed in the united states",
        "printed in england",
        "printed in",
        "macmillan company",
        "macmillan co",
        "macmillan",
        "constance garnett",
        "the novels of",
        "and other stories by",
        "and other tales by",
        "all rights reserved",
        "copyright",
        "publisher",
        "publishers",
        "publishing",
        "limited",
        "ltd.",
        " inc.",
        "press",
        "vintage books",
        "penguin",
        "random house",
        "harpercollins",
        "fyodor dostoevsky",
        "fyodor dostoyevsky",
        "f. m. dostoevsky",
        "fiodor dostoevsky",
        "dostoevsky",
        "dostoyevsky",
    )
    return any(x in n for x in needles)


def should_exclude_from_exported_toc(text: str | None) -> bool:
    """TOCExport filter — imprint lines must not appear as dotted TOC entries."""
    return _looks_like_title_imprint_or_series_boilerplate(text)


def _looks_like_probable_toc_listing_row(text: str | None) -> bool:
    """Printed TOC leaf line (dots or trailing page num), excluding imprint clutter."""
    if not text:
        return False
    if _looks_like_title_imprint_or_series_boilerplate(text):
        return False
    t = text.strip()
    if len(t) > 240:
        return False
    if re.search(r"\.{3,}|\u2026", t):
        return True
    if re.search(r"\s\d{1,4}\s*$", t):
        return True
    if len(t) < 140 and re.search(r"\s+[ivxlcdm]{1,12}\s*$", t, flags=re.IGNORECASE):
        return True
    # Notes-style TOC pointers without a leaf digit on the same extract line
    if len(t) < 120 and re.search(r"\[\d+\]", t):
        return True
    # Part / chapter labels that appear only as TOC stubs (short, no prose)
    if len(t) < 96 and re.match(
        r"(?is)^(part\s+[ivxlcdm\d]{1,8}|chapter\s+\d{1,3})\b",
        t,
    ):
        return True
    return False


def _looks_like_toc_body_opener_paragraph(text: str) -> bool:
    """True when a paragraph starts real Preface/Introduction narrative, not a TOC listing row."""
    t = text.strip()
    if len(t) < 24:
        return False
    # TOC rows: dot leaders / Unicode ellipsis bridges to page numbers.
    if re.search(r"\.{3,}|\u2026", t):
        return False
    # Leaf page column (arabic or roman)
    if len(t) < 260 and (
        re.search(r"\s\d{1,4}\s*$", t)
        or re.search(r"\s+[ivxlcdm]{1,12}\s*$", t, flags=re.IGNORECASE)
    ):
        return False
    low = t.lower()
    if low.startswith("preface ") and count_words(t) >= 10:
        return True
    # Introduction TOC titles are usually short; body copy needs more words.
    if re.match(r"(?is)^introduction\s*[:\.]?\s*\S+", t) and count_words(t) >= 18:
        return True
    return False


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

    for i in range(n):
        block = blocks[i]

        # Any real heading after Contents (chapter / section opener) ends TOC capture —
        # avoids tagging FIRST NIGHT / WHITE NIGHTS as TOC entries because outline level >
        # Contents heading level.
        if in_toc and block.type == BlockType.HEADING and block.text:
            if not _toc_heading_triggers(block.text):
                in_toc = False

        if not in_toc and block.type == BlockType.HEADING and block.text:
            if _toc_heading_triggers(block.text):
                in_toc = True
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
                blocks[i] = block.model_copy(update={"structural_tag": StructuralTag.TOC})
                continue

        if in_toc:
            if block.type in (BlockType.PARAGRAPH, BlockType.LIST) and block.text:
                t = block.text.strip()
                if _looks_like_title_imprint_or_series_boilerplate(t):
                    in_toc = False
                    continue
                if count_words(t) >= 40 or len(t) >= 320:
                    in_toc = False
                    continue
                if _looks_like_toc_body_opener_paragraph(t):
                    in_toc = False
                    continue
                if _looks_like_probable_toc_listing_row(t):
                    blocks[i] = block.model_copy(update={"structural_tag": StructuralTag.TOC})
                    continue
                # Short filler without TOC leaf cues (title-page spillover).
                in_toc = False
                continue
            if block.type == BlockType.TABLE:
                blocks[i] = block.model_copy(update={"structural_tag": StructuralTag.TOC})
                continue
            # HEADING should have cleared TOC above; LIST/other — stop TOC tagging.
            in_toc = False
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
