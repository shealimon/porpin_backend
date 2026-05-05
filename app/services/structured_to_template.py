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
from app.services.formatter.chapter_heading_policy import (
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


def _append_paragraph_dedup(blocks: list[_Block], text: str) -> None:
    """Skip immediate duplicate paragraphs to avoid PDF spam from noisy parsers/OCR."""
    t = _normalize_text(text)
    if not t:
        return
    if blocks and isinstance(blocks[-1], BlockParagraphModel):
        if _normalize_text(blocks[-1].text) == t:
            return
    blocks.append(BlockParagraphModel(text=t))


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
        return [
            {
                "text": h.text,
                "anchor": h.anchor,
                "level": max(2, min(6, int(h.level or 2))),
            }
            for h in chosen
        ]

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

    return out


def _outline_levels_from_structured(doc: StructuredDocument) -> list[int]:
    return [
        raw.level
        for raw in doc.content
        if isinstance(raw, StructuredHeading) and raw.content_tag != "toc"
    ]


def structured_to_document_for_template(doc: StructuredDocument) -> DocumentForTemplate:
    """Map normalized structure to the API model used by server-side and client document templates."""
    title = _normalize_text(doc.title or "") or "Document"
    outline_lvls = _outline_levels_from_structured(doc)
    chapter_lvl = chapter_start_level(outline_lvls)
    blocks: list[_Block] = []
    used_anchors: set[str] = set()
    for line in doc.authors:
        t = _normalize_text(line or "")
        if t:
            _append_paragraph_dedup(blocks, t)
    for raw in doc.content:
        # Do not render extracted TOC text verbatim; we generate a clean TOC from real headings.
        if getattr(raw, "content_tag", None) == "toc":
            continue
        if isinstance(raw, StructuredHeading):
            heading_text = _normalize_text(raw.text)
            if not heading_text:
                continue
            base = _slugify(heading_text)
            anchor = _unique_anchor(base, used_anchors)
            blocks.append(
                BlockHeadingModel(
                    text=heading_text,
                    level=_heading_level_to_template_level(raw.level),
                    chapter_start=is_chapter_outline_level(
                        raw.level, chapter_lvl, heading_text=heading_text
                    ),
                    anchor=anchor,
                )
            )
        elif isinstance(raw, StructuredParagraph):
            p = _normalize_text(raw.text)
            if p:
                _append_paragraph_dedup(blocks, p)
        elif isinstance(raw, StructuredList):
            blocks.append(
                BlockListModel(
                    items=[_normalize_text(x) for x in list(raw.items) if _normalize_text(x)],
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
    return DocumentForTemplate(title=title, blocks=blocks)


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

    # Build a TOC from the final rendered block list (post-normalization). This guarantees every
    # TOC href points to an existing `id=...` in the emitted HTML.
    from app.services.document_template_render.render import _get_jinja_env  # type: ignore
    from app.services.document_template_render.css import indented_css_for_template
    from app.services.document_template_render.context import build_template_context

    ctx = build_template_context(m, resolved, indented_css_for_template(resolved))

    toc: list[dict[str, object]] = []
    try:
        # `ctx["blocks"]` is a list of dicts (normalized for template view).
        if ctx.get("use_chapters"):
            # Chapters mode is rare for structured docs, but handle it anyway.
            # Only TOC chapter titles (and optional one-level subheads) are supported here.
            chapters = list(ctx.get("chapters") or [])
            for ch in chapters:
                title = str((ch or {}).get("displayTitle") or (ch or {}).get("title") or "").strip()
                anchor = str((ch or {}).get("anchor") or "").strip()
                if title and anchor:
                    toc.append({"text": title, "anchor": anchor, "level": 2})
        else:
            blocks_view = list(ctx.get("blocks") or [])
            # Reuse the same TOC policy, but operate on the final block dicts.
            headings: list[dict[str, object]] = [
                b
                for b in blocks_view
                if isinstance(b, dict)
                and b.get("type") == "heading"
                and str(b.get("text") or "").strip()
                and str(b.get("anchor") or "").strip()
            ]
            # Convert to temporary BlockHeadingModel-like objects is overkill; replicate policy.
            chapter_heads = [h for h in headings if bool(h.get("chapterStart"))]
            if not chapter_heads:
                min_lvl = min(int(h.get("hLevel") or 2) for h in headings) if headings else 2
                chosen = [h for h in headings if int(h.get("hLevel") or 2) == min_lvl]
            else:
                chapter_lvl = min(int(h.get("hLevel") or 2) for h in chapter_heads)
                sub_lvl = min(6, chapter_lvl + 1)
                chosen = []
                current_chapter_open = False
                for h in headings:
                    lvl = int(h.get("hLevel") or 2)
                    is_chapter = bool(h.get("chapterStart")) and lvl == chapter_lvl
                    if is_chapter:
                        current_chapter_open = True
                    elif current_chapter_open:
                        if lvl != sub_lvl:
                            continue
                    if not is_chapter and not current_chapter_open:
                        continue
                    chosen.append(h)
            seen: set[str] = set()
            for h in chosen[:120]:
                a = str(h.get("anchor") or "").strip()
                if not a or a in seen:
                    continue
                seen.add(a)
                toc.append(
                    {
                        "text": str(h.get("text") or "").strip(),
                        "anchor": a,
                        "level": max(2, min(6, int(h.get("hLevel") or 2))),
                    }
                )
    except Exception:
        toc = []

    if toc:
        ctx["toc"] = toc
    return _get_jinja_env().get_template("layout.j2").render(**ctx)
