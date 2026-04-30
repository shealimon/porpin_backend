"""Build template context to mirror ``buildTemplateContext`` in the frontend."""

from __future__ import annotations

import re
from typing import Any, Literal

from app.services.document_template_render.models import (
    BlockHeadingModel,
    BlockListModel,
    BlockParagraphModel,
    DocumentChapterModel,
    DocumentForTemplate,
    DocumentTemplateType,
)

DEFAULT_LEVEL: Literal[2, 3] = 2

_NUM_ONLY = re.compile(r"^\s*(\d{1,3})\s*$")


def _h_level(heading: BlockHeadingModel) -> int:
    return int(heading.level or DEFAULT_LEVEL)


_CHAPTER_PREFIX = re.compile(
    r"^\s*(?:chapter|chap)\s*(\d+)\s*[:.\-–—]?\s*(.*)\s*$",
    re.IGNORECASE,
)


def _split_chapter_title(text: str | None) -> tuple[int | None, str | None]:
    """If the title looks like 'Chapter 1: Title', return (1, 'Title').

    If it is only 'Chapter 1', return (1, None) to avoid duplicating number + title.
    Otherwise return (None, original_text).
    """
    t = (text or "").strip()
    if not t:
        return (None, None)
    m = _CHAPTER_PREFIX.match(t)
    if not m:
        return (None, t)
    try:
        n = int(m.group(1))
    except Exception:
        n = None
    rest = (m.group(2) or "").strip() or None
    return (n, rest)


def _chapter_to_view(ch: DocumentChapterModel) -> dict[str, Any]:
    override_no, display = _split_chapter_title(ch.title)
    return {
        "title": ch.title,
        "chapterNo": override_no,
        "displayTitle": display,
        "headings": [
            {
                "text": h.text,
                "hLevel": int(h.level or DEFAULT_LEVEL),
                **({"anchor": h.anchor} if getattr(h, "anchor", None) else {}),
            }
            for h in ch.headings
        ],
        "paragraphs": list(ch.paragraphs),
        "lists": [
            {"items": list(l.items), "ordered": l.ordered} for l in ch.lists
        ],
    }


def _block_to_view(
    b: BlockHeadingModel | BlockParagraphModel | BlockListModel,
) -> dict[str, Any]:
    if isinstance(b, BlockHeadingModel):
        out: dict[str, Any] = {
            "type": "heading",
            "text": b.text,
            "hLevel": _h_level(b),
        }
        if b.chapter_start:
            override_no, display = _split_chapter_title(b.text)
            out["chapterStart"] = True
            if override_no is not None:
                out["chapterNo"] = override_no
            if display is not None:
                out["displayText"] = display
        if b.anchor:
            out["anchor"] = b.anchor
        return out
    if isinstance(b, BlockParagraphModel):
        return {"type": "paragraph", "text": b.text}
    return {
        "type": "list",
        "items": list(b.items),
        "ordered": b.ordered,
    }


def _flat_blocks(doc: DocumentForTemplate) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for h in doc.headings or []:
        out.append(
            {
                "type": "heading",
                "text": h.text,
                "hLevel": int(h.level or DEFAULT_LEVEL),
            }
        )
    for p in doc.paragraphs or []:
        out.append({"type": "paragraph", "text": p})
    for lst in doc.lists or []:
        out.append(
            {
                "type": "list",
                "items": list(lst.items),
                "ordered": lst.ordered,
            }
        )
    return out


def build_template_context(
    model: DocumentForTemplate,
    template_id: DocumentTemplateType,
    css: str,
) -> dict[str, Any]:
    """Data for `layout.j2` (title, themeId, css, use_chapters, chapters, blocks)."""
    title = model.title
    if model.chapters is not None:
        # Defensive: some callers may accidentally provide both `chapters` and `blocks`.
        # Prefer chapters only when they contain meaningful content; otherwise fall back to blocks.
        chapters_view = [_chapter_to_view(c) for c in model.chapters]
        has_meaningful_chapters = any(
            bool(c.get("headings"))
            or bool(c.get("paragraphs"))
            or bool(c.get("lists"))
            for c in chapters_view
        )
        if has_meaningful_chapters:
            return {
                "title": title,
                "themeId": template_id,
                "css": css,
                "use_chapters": True,
                "chapters": chapters_view,
                "blocks": [],
            }
    if model.blocks is not None:
        blocks_view: list[dict[str, Any]] = []
        i = 0
        blocks = list(model.blocks)
        while i < len(blocks):
            v = _block_to_view(blocks[i])

            # Special case: some extractors split a chapter heading into:
            #   Heading (chapterStart): "1"
            #   Heading (non-chapterStart): "Who Is Your Imagination"
            # Rendering that naively yields "1" as the chapter number AND "1" as the title,
            # with the real title pushed down as a subheading.
            if (
                v.get("type") == "heading"
                and v.get("chapterStart")
                and isinstance(v.get("text"), str)
                and _NUM_ONLY.match(str(v.get("text") or ""))
                and i + 1 < len(blocks)
            ):
                nxt = _block_to_view(blocks[i + 1])
                if (
                    nxt.get("type") == "heading"
                    and not nxt.get("chapterStart")
                    and isinstance(nxt.get("text"), str)
                    and str(nxt.get("text") or "").strip()
                ):
                    try:
                        v["chapterNo"] = int(str(v.get("text") or "").strip())
                    except Exception:
                        pass
                    v["displayText"] = str(nxt.get("text") or "").strip()
                    v["text"] = f"{v.get('text', '')} {v['displayText']}".strip()
                    i += 2
                else:
                    i += 1
            else:
                i += 1

            # Merge consecutive chapter-start headings (common when a PDF extractor splits a
            # single title across lines, e.g. "The Pruning Shears of" + "Revision").
            if (
                v.get("type") == "heading"
                and v.get("chapterStart")
                and blocks_view
                and blocks_view[-1].get("type") == "heading"
                and blocks_view[-1].get("chapterStart")
            ):
                prev = blocks_view[-1]
                prev["text"] = f"{prev.get('text', '')} {v.get('text', '')}".strip()
                if "displayText" in prev or "displayText" in v:
                    a = str(prev.get("displayText") or prev.get("text") or "").strip()
                    btxt = str(v.get("displayText") or v.get("text") or "").strip()
                    merged = f"{a} {btxt}".strip()
                    if merged:
                        prev["displayText"] = merged
                continue
            blocks_view.append(v)
        return {
            "title": title,
            "themeId": template_id,
            "css": css,
            "use_chapters": False,
            "chapters": [],
            "blocks": blocks_view,
        }
    return {
        "title": title,
        "themeId": template_id,
        "css": css,
        "use_chapters": False,
        "chapters": [],
        "blocks": _flat_blocks(model),
    }
