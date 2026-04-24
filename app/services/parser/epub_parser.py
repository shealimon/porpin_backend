"""EPUB → structured blocks (HTML in spine items)."""

from __future__ import annotations

import re
from pathlib import Path

import ebooklib
from bs4 import BeautifulSoup
from ebooklib import epub

from app.models.document_models import BlockType, ContentBlock


def parse_epub(path: Path) -> list[ContentBlock]:
    book = epub.read_epub(str(path))
    blocks: list[ContentBlock] = []
    for item in book.get_items():
        if item.get_type() != ebooklib.ITEM_DOCUMENT:
            continue
        raw = item.get_content()
        if not raw:
            continue
        try:
            html = raw.decode("utf-8", errors="replace")
        except Exception:
            continue
        soup = BeautifulSoup(html, "html.parser")
        for el in soup.find_all(["h1", "h2", "h3", "h4", "h5", "h6", "p", "table"]):
            tag = el.name.lower()
            if tag.startswith("h") and len(tag) == 2 and tag[1].isdigit():
                level = int(tag[1])
                text = _norm_text(el.get_text(" ", strip=True))
                if text:
                    blocks.append(
                        ContentBlock(type=BlockType.HEADING, text=text, level=level)
                    )
            elif tag == "p":
                text = _norm_text(el.get_text(" ", strip=True))
                if text:
                    blocks.append(ContentBlock(type=BlockType.PARAGRAPH, text=text))
            elif tag == "table":
                rows = _table_from_html(el)
                if rows:
                    blocks.append(ContentBlock(type=BlockType.TABLE, data=rows))
    return _merge_adjacent_paragraphs(blocks)


def _norm_text(s: str) -> str:
    t = re.sub(r"\s+", " ", s or "").strip()
    return t


def _table_from_html(table) -> list[list[str]]:
    rows: list[list[str]] = []
    for tr in table.find_all("tr"):
        cells = tr.find_all(["td", "th"])
        if not cells:
            continue
        rows.append([_norm_text(c.get_text(" ", strip=True)) for c in cells])
    return [r for r in rows if any(cell for cell in r)]


def _merge_adjacent_paragraphs(blocks: list[ContentBlock], max_merge: int = 3) -> list[ContentBlock]:
    out: list[ContentBlock] = []
    i = 0
    while i < len(blocks):
        b = blocks[i]
        if b.type != BlockType.PARAGRAPH or not b.text:
            out.append(b)
            i += 1
            continue
        parts = [b.text]
        j = i + 1
        while j < len(blocks) and j - i < max_merge:
            n = blocks[j]
            if n.type != BlockType.PARAGRAPH or not n.text:
                break
            parts.append(n.text)
            j += 1
        if j - i > 1:
            out.append(ContentBlock(type=BlockType.PARAGRAPH, text="\n\n".join(parts)))
            i = j
        else:
            out.append(b)
            i += 1
    return out
