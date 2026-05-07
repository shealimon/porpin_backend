"""DOCX → structured blocks (headings, paragraphs, lists, tables)."""

from __future__ import annotations

import re
import time
from pathlib import Path

from docx import Document
from docx.table import Table
from docx.text.paragraph import Paragraph

from app.models.document_models import BlockType, ContentBlock, ListKind


def _word_count_str(s: str) -> int:
    return len(re.findall(r"\S+", (s or "").strip()))


def _trim_words(s: str, max_words: int) -> str:
    words = re.findall(r"\S+", (s or "").strip())
    if len(words) <= max_words:
        return (s or "").strip()
    return " ".join(words[:max_words])


def parse_docx(
    path: Path,
    timings: dict[str, float] | None = None,
    max_preview_words: int | None = None,
) -> list[ContentBlock]:
    t0 = time.perf_counter()
    doc = Document(str(path))
    if timings is not None:
        timings["docx_document_open_s"] = time.perf_counter() - t0
    t1 = time.perf_counter()
    blocks: list[ContentBlock] = []
    used = 0
    budget = max_preview_words
    body = doc.element.body
    for child in body:
        if budget is not None and used >= budget:
            break
        tag = child.tag.split("}")[-1] if "}" in child.tag else child.tag
        if tag == "tbl":
            table = Table(child, doc)
            for block in _table_blocks(table):
                w = _words_in_block(block)
                if budget is None:
                    blocks.append(block)
                    continue
                remain = budget - used
                if remain < 1:
                    break
                if w <= remain:
                    blocks.append(block)
                    used += w
                else:
                    trimmed = _trim_table_block(block, remain)
                    if trimmed is not None:
                        blocks.append(trimmed)
                        used = budget
                    break
            else:
                continue
            break
        if tag == "p":
            para = Paragraph(child, doc)
            block = _paragraph_to_block(para)
            if block is None:
                continue
            w = _words_in_block(block)
            if budget is None:
                blocks.append(block)
                continue
            remain = budget - used
            if remain < 1:
                break
            if w <= remain:
                blocks.append(block)
                used += w
            else:
                trimmed = _trim_text_block(block, remain)
                if trimmed is not None:
                    blocks.append(trimmed)
                break
    if timings is not None:
        timings["docx_body_walk_extract_s"] = time.perf_counter() - t1
    return blocks


def _words_in_block(block: ContentBlock) -> int:
    if block.text:
        return _word_count_str(block.text)
    if block.data:
        return sum(_word_count_str(c) for row in block.data for c in row if c)
    return 0


def _trim_text_block(block: ContentBlock, max_words: int) -> ContentBlock | None:
    if not block.text:
        return None
    t = _trim_words(block.text, max_words)
    if not t:
        return None
    return block.model_copy(update={"text": t})


def _trim_table_block(block: ContentBlock, max_words: int) -> ContentBlock | None:
    if not block.data:
        return None
    rows: list[list[str]] = []
    used = 0
    for row in block.data:
        if used >= max_words:
            break
        new_row: list[str] = []
        for cell in row:
            if used >= max_words:
                new_row.append("")
                continue
            raw = cell or ""
            cw = _word_count_str(raw)
            if used + cw <= max_words:
                new_row.append(raw)
                used += cw
            else:
                new_row.append(_trim_words(raw, max_words - used))
                used = max_words
        rows.append(new_row)
    if not rows or not any(c.strip() for row in rows for c in row):
        return None
    return block.model_copy(update={"data": rows})


def _infer_list_kind(text: str) -> ListKind:
    first_line = (text or "").lstrip().split("\n", 1)[0].lstrip()
    if re.match(r"^\d+[\.)]\s+", first_line):
        return "ordered"
    return "bullet"


def _para_explicit_bold_char_fraction(para: Paragraph) -> float:
    """Share of characters in runs marked bold=True (Theme/font inheritance ignored)."""
    total = 0
    bold = 0
    for r in para.runs:
        raw = r.text or ""
        if not raw.strip():
            continue
        total += len(raw)
        if r.bold is True:
            bold += len(raw)
    if total <= 0:
        return 0.0
    return bold / total


def _paragraph_to_block(para: Paragraph) -> ContentBlock | None:
    text = para.text.strip()
    if not text:
        return None
    style = (para.style.name or "").lower()
    if "heading" in style:
        level = _heading_level_from_style(para.style.name)
        return ContentBlock(type=BlockType.HEADING, text=text, level=level)
    if para._p.pPr is not None and para._p.pPr.numPr is not None:
        return ContentBlock(
            type=BlockType.LIST,
            text=text,
            list_kind=_infer_list_kind(text),
        )
    wc = _word_count_str(text)
    bold_frac = _para_explicit_bold_char_fraction(para)
    if bold_frac >= 0.82 and 1 <= wc <= 15:
        level = 2 if wc <= 6 and bold_frac >= 0.9 else 3
        return ContentBlock(type=BlockType.HEADING, text=text, level=level)
    return ContentBlock(type=BlockType.PARAGRAPH, text=text)


def _heading_level_from_style(style_name: str) -> int:
    import re

    m = re.search(r"(\d+)", style_name or "")
    if m:
        return max(1, min(9, int(m.group(1))))
    return 1


def _table_blocks(table: Table) -> list[ContentBlock]:
    rows: list[list[str]] = []
    for row in table.rows:
        cells = [c.text.strip().replace("\n", " ") for c in row.cells]
        rows.append(cells)
    if not rows:
        return []
    return [ContentBlock(type=BlockType.TABLE, data=rows)]
