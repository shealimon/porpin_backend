"""Free preview: slice parsed blocks to the first few pages (PDF) or word budget (DOCX/TXT)."""

from __future__ import annotations

import re
from pathlib import Path

import fitz

from app.core.pipeline_settings import get_pipeline_settings
from app.models.document_models import BlockType, ContentBlock


def count_pdf_pages(path: Path) -> int:
    doc = fitz.open(path)
    try:
        return len(doc)
    finally:
        doc.close()


def estimate_document_pages(path: Path, fast_word_count: int) -> int:
    """Rough page count: exact for PDF, else words / configured words-per-page."""
    sfx = path.suffix.lower()
    if sfx == ".pdf":
        return max(1, count_pdf_pages(path))
    wpp = max(50, int(get_pipeline_settings().translation_preview_words_per_page_estimate))
    return max(1, (max(0, int(fast_word_count)) + wpp - 1) // wpp)


def preview_eligibility(path: Path, fast_word_count: int) -> tuple[bool, int, int]:
    """Return (eligible, estimated_pages, preview_page_cap).

    Preview is offered only when the document has *more* pages than the preview depth
    (e.g. >3 pages when cap is 3) so a short PDF cannot be fully read for free.
    """
    settings = get_pipeline_settings()
    cap = max(1, int(settings.translation_preview_max_pages))
    pages = estimate_document_pages(path, fast_word_count)
    return pages > cap, pages, cap


def block_word_count(block: ContentBlock) -> int:
    n = 0
    if block.text:
        n += len(re.findall(r"\S+", block.text))
    if block.data:
        for row in block.data:
            for cell in row:
                if cell:
                    n += len(re.findall(r"\S+", cell))
    return n


def truncate_blocks_to_word_budget(
    blocks: list[ContentBlock],
    *,
    max_words: int,
) -> list[ContentBlock]:
    """Keep leading blocks until ``max_words``; may trim the last block (text or table cells)."""
    if max_words < 1:
        return []
    out: list[ContentBlock] = []
    used = 0
    for b in blocks:
        w = block_word_count(b)
        if used + w <= max_words:
            out.append(b)
            used += w
            continue
        remain = max_words - used
        if remain < 1:
            break
        trimmed = _trim_block_to_words(b, remain)
        if trimmed is not None:
            out.append(trimmed)
        break
    return out


def _trim_plain_text(text: str, max_words: int) -> str:
    words = re.findall(r"\S+", (text or "").strip())
    if len(words) <= max_words:
        return (text or "").strip()
    return " ".join(words[:max_words])


def _trim_table_data(data: list[list[str]], max_words: int) -> list[list[str]]:
    out: list[list[str]] = []
    used = 0
    for row in data:
        if used >= max_words:
            break
        new_row: list[str] = []
        for cell in row:
            if used >= max_words:
                new_row.append("")
                continue
            raw = cell or ""
            cell_words = re.findall(r"\S+", raw)
            if used + len(cell_words) <= max_words:
                new_row.append(raw)
                used += len(cell_words)
            else:
                take = max_words - used
                new_row.append(_trim_plain_text(raw, take))
                used = max_words
        out.append(new_row)
    return out


def _trim_block_to_words(block: ContentBlock, max_words: int) -> ContentBlock | None:
    if max_words < 1:
        return None
    if block.type == BlockType.TABLE and block.data:
        trimmed = _trim_table_data(block.data, max_words)
        if not trimmed or not any(c.strip() for row in trimmed for c in row):
            return None
        return block.model_copy(update={"data": trimmed})
    if block.text is not None:
        t = _trim_plain_text(block.text, max_words)
        if not t:
            return None
        return block.model_copy(update={"text": t})
    return None


def split_blocks_for_preview(
    blocks: list[ContentBlock],
    path: Path,
    *,
    max_pages: int,
) -> tuple[list[ContentBlock], list[ContentBlock]]:
    """First tuple: blocks to translate for preview; second: remainder (not translated in preview)."""
    sfx = path.suffix.lower()
    if sfx == ".pdf":
        preview = [
            b
            for b in blocks
            if b.source_page is not None and b.source_page <= max_pages
        ]
        rest = [
            b
            for b in blocks
            if b.source_page is None or b.source_page > max_pages
        ]
        return preview, rest

    wpp = max(50, int(get_pipeline_settings().translation_preview_words_per_page_estimate))
    budget = max(1, int(max_pages) * wpp)
    preview: list[ContentBlock] = []
    used = 0
    i = 0
    for i, b in enumerate(blocks):
        w = block_word_count(b)
        if preview and used + w > budget:
            return preview, blocks[i:]
        preview.append(b)
        used += w
        if used >= budget:
            return preview, blocks[i + 1 :]
    return preview, []
