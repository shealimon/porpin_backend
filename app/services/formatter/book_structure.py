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


def looks_like_printed_toc_leader_row(text: str | None) -> bool:
    """True for classic printed Contents lines: dot leaders + leaf page (roman or arabic).

    Use this to strip verbatim TOC from PDF/HTML body when tagging missed a region.
    """
    if not text or _looks_like_title_imprint_or_series_boilerplate(text):
        return False
    t = " ".join((text or "").split()).strip()
    if len(t) > 260:
        return False
    if not re.search(r"\.{3,}|\u2026", t):
        return False
    if not re.search(
        r"(?:\d{1,4}|[ivxlcdm]{1,12})\s*$", t, flags=re.IGNORECASE
    ):
        return False
    return True


def looks_like_printed_toc_heading_noise(text: str | None) -> bool:
    """Heading text that came from a printed Contents row, not a real body chapter opener.

    PDF extractors emit these as ``Heading`` blocks; they match ``chapter_like_heading_text``
    (``Part …``, ``Chapter …``) so they incorrectly become chapter pages **and** duplicate the
    auto-generated Contents next to real part/chapter headings.
    """
    if not text or _looks_like_title_imprint_or_series_boilerplate(text):
        return False
    if looks_like_printed_toc_leader_row(text):
        return True
    t = " ".join((text or "").split()).strip()
    if len(t) > 220:
        return False
    # "Chapter 1" / "Part 3" — trailing digit is the label, not a printed TOC page column.
    if re.match(r"(?i)^chapter\s+\d{1,3}\s*$", t) or re.match(
        r"(?i)^part\s+[ivxlcdm]{1,8}\s*$", t
    ):
        return False
    if re.match(r"(?i)^part\s+\d{1,3}\s*$", t):
        return False
    has_leaf = bool(
        re.search(r"(?:\d{1,4}|[ivxlcdm]{1,12})\s*$", t, flags=re.IGNORECASE)
    )
    dotty = bool(re.search(r"\.{2,}|\u2026", t))
    if has_leaf and dotty and len(t) < 200 and re.match(
        r"(?i)^(part|chapter|appendix)\s+\d+", t
    ):
        return True
    if has_leaf and len(t) < 200 and re.match(r"(?i)^(part|chapter|appendix)\s+\d+", t):
        if dotty or count_words(t) <= 16:
            return True
    if (
        has_leaf
        and len(t) < 200
        and count_words(t) <= 18
        and re.match(
            r"(?i)^(conclusion|preface|introduction)\b", t,
        )
    ):
        return True
    return False


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


def _heading_looks_like_toc_row_when_in_contents(text: str | None) -> bool:
    """Printed TOC lines are often extracted as HEADING, not paragraph — keep TOC zone alive.

    Without this, the first ``CHAPTER 1.1``-style row clears ``in_toc`` and every following
    TOC page is emitted as body, so the auto-generated ``Contents`` block appears multiple times.
    """
    if not text:
        return False
    if _looks_like_probable_toc_listing_row(text):
        return True
    t = text.strip()
    if len(t) > 200:
        return False
    low = t.lower()
    # Stubs: "chapter 1.5", "PART III"
    if len(t) <= 40 and re.match(
        r"^(chapter|part)\s+(\d+|\d+\.\d+|[ivxlcdm]{1,8})\s*$",
        low,
    ):
        return True
    # Short labeled row (TOC) vs long part opener ("Part 1. The Enemies of Clear Thinking …")
    if re.match(r"(?i)^(chapter|part)\s+(\d+|\d+\.\d+|[ivxlcdm]{1,8})\b", t):
        if count_words(t) <= 12 and len(t) <= 95:
            return True
    if re.match(r"^\d+\.\d+\b", t) and count_words(t) <= 14 and len(t) <= 120:
        return True
    return False


def _looks_like_merged_flat_toc_paragraph(text: str | None) -> bool:
    """PDF/epub spill: TOC or nav spine merged into one paragraph (no dot leaders).

    These must not be translated as body or appear after the generated HTML TOC.
    Covers (1) short *Cover … Chapter 1* spine and (2) long *Chapter 2…16 + Acknowledgments* dumps.
    """
    if not text:
        return False
    t = text.strip()
    if not t:
        return False
    low = t.lower()
    wc = count_words(t)
    # Trade / self-help inline TOC: many question-title chapters in one blob.
    if len(t) >= 70 and t.count("?") >= 3:
        return True
    if len(t) >= 100 and t.count("?") >= 2 and wc >= 40:
        return True
    chapter_hits = len(re.findall(r"(?i)\bchapter\s+\d+", t))

    # Long chapter inventory (typical second spill: Ch 2 … Ch 16, maybe + Acknowledgments)
    if chapter_hits >= 6 and len(t) >= 100:
        return True
    if chapter_hits >= 5 and len(t) >= 140 and re.search(
        r"(?i)\b(?:acknowledg|notes)\b", low
    ):
        return True

    # Original: many chapter tokens in one lump
    if chapter_hits >= 4 and len(t) >= 72:
        return True

    # Short front-matter spine: Cover … Preface … Chapter 1 (no Chapter 2 in same blob)
    if (
        len(t) >= 45
        and chapter_hits >= 1
        and re.search(r"(?i)\bcover\b", low)
        and re.search(r"(?i)\bchapter\s+1\b", t)
        and not re.search(r"(?i)\bchapter\s+2\b", t)
        and (
            re.search(r"(?i)dedication|preface", low)
            or re.search(r"(?i)title\s+page", low)
        )
    ):
        return True

    if len(t) < 160:
        return False
    if chapter_hits >= 3 and any(
        k in low
        for k in (
            "cover",
            "dedication",
            "preface",
            "contents",
            "title page",
            "acknowledgment",
            "acknowledgement",
        )
    ):
        return True
    if (
        re.search(r"(?i)\bcover\b", low)
        and re.search(r"(?i)title\s+page", low)
        and chapter_hits >= 2
    ):
        return True
    return False


def looks_like_merged_toc_body_spill(text: str | None) -> bool:
    """Public helper: paragraphs to omit from themed PDF/DOCX body (works on translated text too)."""
    return _looks_like_merged_flat_toc_paragraph(text)


def _paragraph_starts_contents_t_region(text: str | None) -> bool:
    """Multi-line PDF block: first line is ``Contents``, following lines look like TOC rows."""
    if not text:
        return False
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not lines:
        return False
    head = lines[0]
    if not _toc_heading_triggers(head) or len(head) > 56:
        return False
    if len(lines) == 1:
        return True
    body = lines[1]
    return bool(
        looks_like_printed_toc_leader_row(body)
        or _looks_like_probable_toc_listing_row(body)
    )


def _tag_orphan_printed_toc_runs(blocks: list[ContentBlock]) -> None:
    """Tag repeated printed TOC pages that never saw a ``Contents`` heading (PDF reflow).

    Requires several leader rows so real body pages are not swallowed.
    """
    n = len(blocks)
    i = 0
    while i < n:
        run: list[int] = []
        leader_rows = 0
        j = i
        while j < n:
            b = blocks[j]
            if b.type not in (BlockType.PARAGRAPH, BlockType.HEADING):
                break
            raw = (b.text or "").strip()
            if not raw:
                break
            if b.structural_tag in (StructuralTag.TITLE, StructuralTag.AUTHOR):
                break
            t = raw
            if looks_like_printed_toc_leader_row(t):
                leader_rows += 1
                run.append(j)
                j += 1
                continue
            is_lbl = _toc_heading_triggers(t) or (
                _toc_standalone_line(t) and len(t) < 100
            )
            if is_lbl and not run:
                run.append(j)
                j += 1
                continue
            if (
                run
                and leader_rows >= 1
                and _looks_like_probable_toc_listing_row(t)
                and len(t) < 220
            ):
                if re.search(r"\s\d{1,4}\s*$", t) or re.search(r"\.{3,}|\u2026", t):
                    run.append(j)
                    j += 1
                    continue
            break
        if leader_rows >= 2 and len(run) >= 3:
            for k in run:
                bb = blocks[k]
                if bb.structural_tag not in (
                    StructuralTag.TITLE,
                    StructuralTag.AUTHOR,
                ):
                    blocks[k] = bb.model_copy(update={"structural_tag": StructuralTag.TOC})
            i = run[-1] + 1
        else:
            i += 1


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
            if _toc_heading_triggers(block.text):
                blocks[i] = block.model_copy(update={"structural_tag": StructuralTag.TOC})
                continue
            if _heading_looks_like_toc_row_when_in_contents(block.text):
                blocks[i] = block.model_copy(update={"structural_tag": StructuralTag.TOC})
                continue
            in_toc = False

        if not in_toc and block.type == BlockType.HEADING and block.text:
            if _toc_heading_triggers(block.text):
                in_toc = True
                blocks[i] = block.model_copy(update={"structural_tag": StructuralTag.TOC})
                continue

        if not in_toc and block.type in (BlockType.PARAGRAPH, BlockType.LIST) and block.text:
            t = block.text.strip()
            if (
                block.type == BlockType.PARAGRAPH
                and _paragraph_starts_contents_t_region(block.text)
            ):
                in_toc = True
                blocks[i] = block.model_copy(update={"structural_tag": StructuralTag.TOC})
                continue
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
                if _looks_like_merged_flat_toc_paragraph(t):
                    blocks[i] = block.model_copy(update={"structural_tag": StructuralTag.TOC})
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

    # Tag flattened / orphan printed TOC before title-page heuristics — otherwise ``PART 1 … 14``
    # leader rows are misclassified as the document title.
    for j in range(n):
        b = blocks[j]
        if not b.text or b.structural_tag is not None:
            continue
        if b.type not in (BlockType.PARAGRAPH, BlockType.HEADING):
            continue
        if _looks_like_merged_flat_toc_paragraph(b.text):
            blocks[j] = b.model_copy(update={"structural_tag": StructuralTag.TOC})

    _tag_orphan_printed_toc_runs(blocks)

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
