"""Bridge ``StructuredDocument`` → ``DocumentForTemplate`` for Jinja/WeasyPrint HTML and tests."""

from __future__ import annotations

import re

from app.models.structured_document import (
    StructuredDocument,
    StructuredHeading,
    StructuredList,
    StructuredParagraph,
    StructuredTable,
)
from app.services.document_template_render.models import (
    BlockHeadingModel,
    BlockListModel,
    BlockParagraphModel,
    DocumentForTemplate,
)
from app.services.formatter.book_structure import (
    looks_like_merged_toc_body_spill,
    looks_like_printed_toc_heading_noise,
    looks_like_printed_toc_leader_row,
)
from app.services.formatter.book_heading_display import (
    format_book_main_heading_display,
    is_book_milestone_heading_label,
)
from app.services.formatter.chapter_heading_policy import (
    chapter_like_heading_text,
    chapter_start_level,
    is_chapter_outline_level,
)

_Block = BlockHeadingModel | BlockParagraphModel | BlockListModel


def _heading_level_to_template_level(level: int) -> int:
    """Map outline level to HTML heading tier (document title is ``h1`` in the layout)."""
    return max(2, min(6, 1 + int(level)))

_NON_WORD = re.compile(r"[^a-z0-9]+")
_WS = re.compile(r"\s+")


def _slugify(text: str) -> str:
    t = (text or "").strip().lower()
    t = _NON_WORD.sub("-", t)
    t = t.strip("-")
    return t or "section"


def _normalize_text(text: str) -> str:
    """Collapse stray newlines/tabs/spaces into single spaces for clean PDF flow."""
    return _WS.sub(" ", (text or "").strip())


def _split_compacted_decimal_outline_paragraph(text: str) -> list[str] | None:
    """Split PDF-reflowed subsection inventory: ``1.1 A 1.2 B 1.3 C`` → separate items."""
    t = _normalize_text(text)
    if len(t) < 30:
        return None
    if len(re.findall(r"\b\d+\.\d+\b", t)) < 3:
        return None
    parts = re.split(r"\s+(?=\d+\.\d+\s)", t)
    parts = [p.strip() for p in parts if p.strip()]
    if len(parts) < 3:
        return None
    for p in parts:
        if not re.match(r"^\d+\.\d+\b", p):
            return None
    return parts


_LIST_ENUM_PREFIX = re.compile(r"^\d+\s*[\.)]\s+", re.UNICODE)


def _strip_leading_list_enumeration(item: str) -> str:
    t = _normalize_text(item)
    t = _LIST_ENUM_PREFIX.sub("", t).strip()
    return t


def _subsection_major_minor_from_inventory_item(item: str) -> tuple[int, int] | None:
    """``1. 1.1 Title`` / ``1.1 Title`` → ``(1, 1)``."""
    t = _strip_leading_list_enumeration(item)
    m = re.match(r"^(\d+)\.(\d+)\b", t)
    if not m:
        return None
    return int(m.group(1)), int(m.group(2))


def _part_major_from_part_opener_heading(heading: str) -> int | None:
    """``Part 2. Title`` → 2; lone ``1`` / ``3`` on part splash page → that number."""
    t = _normalize_text(heading or "")
    if not t:
        return None
    m = re.match(r"(?i)^part\s+(\d{1,3})\b", t)
    if m:
        return int(m.group(1))
    if re.match(r"^\d{1,3}$", t):
        return int(t)
    return None


def _heading_precedes_part_subsection_inventory(
    heading: StructuredHeading,
    items: list[str],
    ordered: bool,
) -> bool:
    """Part splash pages repeat subsection titles as a list — drop (Contents already lists them)."""
    if not ordered or len(items) < 3:
        return False
    if getattr(heading, "content_tag", None) == "toc":
        return False
    opener = _normalize_text(heading.text or "")
    if not opener:
        return False
    if not (
        re.match(r"(?i)^part\s+\d+", opener) or re.match(r"^\d{1,3}$", opener)
    ):
        return False
    expected_major = _part_major_from_part_opener_heading(opener)
    if expected_major is None:
        return False
    majors: list[int] = []
    for it in items:
        if len(_normalize_text(it)) > 175:
            return False
        mm = _subsection_major_minor_from_inventory_item(it)
        if mm is None:
            return False
        majors.append(mm[0])
    if len(set(majors)) != 1 or majors[0] != expected_major:
        return False
    return True


def _redundant_part_inventory_list_indices(doc: StructuredDocument) -> set[int]:
    """Indices of ``StructuredList`` nodes that only duplicate the outline from Contents."""
    skip: set[int] = set()
    c = doc.content
    for i in range(1, len(c)):
        raw = c[i]
        if not isinstance(raw, StructuredList):
            continue
        if getattr(raw, "content_tag", None) == "toc":
            continue
        prev = c[i - 1]
        if not isinstance(prev, StructuredHeading):
            continue
        if getattr(prev, "content_tag", None) == "toc":
            continue
        items = [_normalize_text(x) for x in raw.items if _normalize_text(x)]
        if _heading_precedes_part_subsection_inventory(prev, items, raw.ordered):
            skip.add(i)
    return skip


def _toc_entry_text_is_prose_noise(text: str) -> bool:
    """Drop body-like lines mistakenly exported as headings from generated Contents."""
    t = (text or "").strip()
    if len(t) < 90:
        return False
    from app.utils.translate_filter import count_words

    wc = count_words(t)
    if wc < 20:
        return False
    punct = t.count(".") + t.count("?") + t.count("!")
    if punct >= 2:
        return True
    if wc >= 48 and punct >= 1:
        return True
    return False


def _finalize_toc_entries(entries: list[dict[str, object]]) -> list[dict[str, object]]:
    """Remove prose headings and duplicate (text, level) pairs from auto Contents."""
    out: list[dict[str, object]] = []
    seen: set[tuple[str, int]] = set()
    for e in entries:
        text = str(e.get("text") or "").strip()
        if not text:
            continue
        if _toc_entry_text_is_prose_noise(text):
            continue
        norm = _normalize_text(text).lower()
        lvl = max(2, min(6, int(e.get("level") or 2)))
        key = (norm, lvl)
        if key in seen:
            continue
        seen.add(key)
        out.append(e)
    return out


def _append_paragraph_dedup(blocks: list[_Block], text: str) -> None:
    """Skip immediate duplicate paragraphs to avoid PDF spam from noisy parsers/OCR."""
    t = _normalize_text(text)
    if not t:
        return
    if blocks and isinstance(blocks[-1], BlockParagraphModel):
        if _normalize_text(blocks[-1].text) == t:
            return
    blocks.append(BlockParagraphModel(text=t))


def _append_body_paragraph_or_outline_list(blocks: list[_Block], text: str) -> None:
    """Paragraph, or a reflowed decimal outline (1.1 … 1.2 …) as an ordered list."""
    t = _normalize_text(text)
    if not t:
        return
    if looks_like_merged_toc_body_spill(t):
        return
    if looks_like_printed_toc_leader_row(t):
        return
    if looks_like_printed_toc_heading_noise(t):
        return
    split = _split_compacted_decimal_outline_paragraph(t)
    if split:
        items = [_normalize_text(x) for x in split if _normalize_text(x)]
        if len(items) >= 3:
            blocks.append(BlockListModel(items=items, ordered=True))
            return
    _append_paragraph_dedup(blocks, t)


def _unique_anchor(base: str, used: set[str]) -> str:
    candidate = base
    i = 2
    while candidate in used:
        candidate = f"{base}-{i}"
        i += 1
    used.add(candidate)
    return candidate


def _toc_entries_from_blocks(blocks: list[_Block]) -> list[dict[str, object]]:
    """Build TOC entries from heading blocks (for PDF: leader dots + page numbers)."""
    headings: list[BlockHeadingModel] = []
    for b in blocks:
        if not isinstance(b, BlockHeadingModel):
            continue
        if not (b.text or "").strip():
            continue
        if not b.anchor:
            continue
        if looks_like_printed_toc_heading_noise(b.text):
            continue
        headings.append(b)

    if not headings:
        return []

    # Goal:
    # - Always include chapter-start headings (top-level sections)
    # - Optionally include immediate subheadings under each chapter (one level deeper),
    #   but avoid including every minor heading which bloats the TOC.
    chapter_heads = [h for h in headings if bool(getattr(h, "chapter_start", False))]

    # If no explicit chapter-start headings exist, fall back to the shallowest heading tier only.
    if not chapter_heads:
        min_lvl = min(int(h.level or 2) for h in headings)
        chosen = [h for h in headings if int(h.level or 2) == min_lvl]
        return _finalize_toc_entries(
            [
                {
                    "text": h.text,
                    "anchor": h.anchor,
                    "level": max(2, min(6, int(h.level or 2))),
                }
                for h in chosen
            ]
        )

    chapter_lvl = min(int(h.level or 2) for h in chapter_heads)
    sub_lvl = min(6, chapter_lvl + 1)

    out: list[dict[str, object]] = []
    seen: set[str] = set()

    # Hard cap to keep PDFs responsive even if extraction produces thousands of headings.
    MAX_ENTRIES = 120

    current_chapter_open = False
    for h in headings:
        lvl = int(h.level or 2)
        is_chapter = bool(getattr(h, "chapter_start", False)) and lvl == chapter_lvl

        if is_chapter:
            current_chapter_open = True
        elif current_chapter_open:
            # Allow immediate subheadings under the current chapter only.
            if lvl != sub_lvl:
                continue

        if not is_chapter and not current_chapter_open:
            continue

        if h.anchor in seen:
            continue
        seen.add(h.anchor)

        out.append(
            {
                "text": h.text,
                "anchor": h.anchor,
                "level": max(2, min(6, lvl)),
            }
        )
        if len(out) >= MAX_ENTRIES:
            break

    return _finalize_toc_entries(out)


def _outline_levels_from_structured(doc: StructuredDocument) -> list[int]:
    return [
        raw.level
        for raw in doc.content
        if isinstance(raw, StructuredHeading) and raw.content_tag != "toc"
    ]


def _heading_render_flags(
    raw: StructuredHeading,
    chapter_lvl: int | None,
) -> tuple[bool, bool, int]:
    """(chapter_start, is_subheading, template_h_level).

    Blocks tagged ``kind="heading"`` from structure detection are **section** titles. They must
    not inherit chapter-opener layout just because their outline level equals ``chapter_lvl`` —
    that was collapsing subsections into chapter pages. Only *chapter-like* wording (or explicit
    ``kind="chapter"``) opens a chapter block.

    Subheadings use a **fixed** deep HTML tier so they never pick up ``doc-heading--3/4`` section
    styles meant for real headings.
    """
    heading_text = _normalize_text(raw.text)
    hk = getattr(raw, "kind", None)
    base_tpl = _heading_level_to_template_level(raw.level)
    if hk == "chapter":
        return True, False, base_tpl
    if hk == "subheading":
        return False, True, 5
    if hk == "heading":
        return chapter_like_heading_text(heading_text), False, base_tpl
    chapter_start = is_chapter_outline_level(
        raw.level,
        chapter_lvl,
        heading_text=heading_text,
    )
    is_sub = int(raw.level) >= 3 and not chapter_start
    return chapter_start, is_sub, base_tpl


def structured_to_document_for_template(doc: StructuredDocument) -> DocumentForTemplate:
    """Map normalized structure to the API model used by server-side and client document templates."""
    title = _normalize_text(doc.title or "") or "Document"
    subtitle = _normalize_text(doc.subtitle or "") or None
    outline_lvls = _outline_levels_from_structured(doc)
    chapter_lvl = chapter_start_level(outline_lvls)
    blocks: list[_Block] = []
    used_anchors: set[str] = set()
    for line in doc.authors:
        t = _normalize_text(line or "")
        if t:
            _append_paragraph_dedup(blocks, t)
    skip_list_idx = _redundant_part_inventory_list_indices(doc)
    for i, raw in enumerate(doc.content):
        if i in skip_list_idx:
            continue
        # Do not render extracted TOC text verbatim; we generate a clean TOC from real headings.
        if getattr(raw, "content_tag", None) == "toc":
            continue
        if isinstance(raw, StructuredHeading):
            heading_text = _normalize_text(raw.text)
            if not heading_text:
                continue
            if looks_like_printed_toc_leader_row(heading_text):
                continue
            if looks_like_printed_toc_heading_noise(heading_text):
                continue
            chapter_start, is_sub, tpl_lvl = _heading_render_flags(raw, chapter_lvl)
            heading_text = format_book_main_heading_display(heading_text)
            base = _slugify(heading_text)
            anchor = _unique_anchor(base, used_anchors)
            milestone_section = (
                is_book_milestone_heading_label(raw.text)
                and not is_sub
                and not chapter_start
            )
            blocks.append(
                BlockHeadingModel(
                    text=heading_text,
                    level=tpl_lvl,
                    chapter_start=chapter_start,
                    is_subheading=is_sub,
                    milestone_section=milestone_section,
                    anchor=anchor,
                )
            )
        elif isinstance(raw, StructuredParagraph):
            p = _normalize_text(raw.text)
            if p:
                if raw.is_quote:
                    blocks.append(BlockParagraphModel(text=p, is_quote=True))
                else:
                    _append_body_paragraph_or_outline_list(blocks, p)
        elif isinstance(raw, StructuredList):
            items = [_normalize_text(x) for x in list(raw.items) if _normalize_text(x)]
            if items and all(looks_like_printed_toc_leader_row(x) for x in items):
                continue
            blocks.append(
                BlockListModel(
                    items=items,
                    ordered=raw.ordered,
                )
            )
        elif isinstance(raw, StructuredTable):
            for row in raw.rows:
                line = " | ".join(_normalize_text(c) for c in row)
                if line.strip():
                    _append_paragraph_dedup(blocks, line)
    if not blocks:
        blocks = [BlockParagraphModel(text="")]
    return DocumentForTemplate(title=title, subtitle=subtitle, blocks=blocks)


def render_structured_document_html(
    doc: StructuredDocument,
    template_id: str | None = None,
    *,
    source_doc: StructuredDocument | None = None,
) -> str:
    """Structured document → Jinja ``layout.j2`` (server-side themes, optional bilingual source column)."""
    from app.services.document_template_render import render_document_html, resolve_template_type
    from app.services.document_template_render.bilingual import render_bilingual_document_html

    resolved = resolve_template_type(template_id)
    if resolved == "bilingual":
        if source_doc is None:
            m = structured_to_document_for_template(doc)
            return render_document_html(m, "report")
        return render_bilingual_document_html(source_doc, doc, resolved)
    m = structured_to_document_for_template(doc)
    return render_document_html(m, template_id)
