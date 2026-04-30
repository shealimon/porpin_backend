"""PDF → structured blocks using PyMuPDF; tables via pdfplumber when available."""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any

import fitz

from app.core.pipeline_settings import get_pipeline_settings
from app.models.document_models import BlockType, ContentBlock

# Cache page dicts for modest page counts to avoid two get_text("dict") passes per page.
_PDF_TEXTDICT_CACHE_MAX_PAGES = 100


def _collect_span_sizes_from_dict(d_v: dict[str, Any]) -> list[float]:
    sizes: list[float] = []
    for block in d_v.get("blocks", []):
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []):
            for sp in line.get("spans", []):
                sz = float(sp.get("size") or 0)
                if sz > 0:
                    sizes.append(sz)
    return sizes


def _median_font_size(sizes: list[float]) -> float:
    if not sizes:
        return 11.0
    s = sorted(sizes)
    return s[len(s) // 2]


def parse_pdf(
    path: Path,
    timings: dict[str, float] | None = None,
    max_pages: int | None = None,
) -> list[ContentBlock]:
    t_load0 = time.perf_counter()
    doc = fitz.open(path)
    blocks: list[ContentBlock] = []
    try:
        n_pages = len(doc)
        page_limit = n_pages if max_pages is None else min(n_pages, max(1, int(max_pages)))
        cache_dicts: list[dict[str, Any]] | None = (
            [] if page_limit <= _PDF_TEXTDICT_CACHE_MAX_PAGES else None
        )
        all_sizes: list[float] = []
        if cache_dicts is not None:
            for page_index in range(page_limit):
                d = doc[page_index].get_text("dict")
                cache_dicts.append(d)
                all_sizes.extend(_collect_span_sizes_from_dict(d))
            body_font = _median_font_size(all_sizes)
        else:
            body_font = _estimate_body_fontsize(doc)

        plumber_pdf = (
            _open_pdfplumber_once(path)
            if get_pipeline_settings().pdf_use_pdfplumber_for_tables
            else None
        )
        if timings is not None:
            timings["pdf_document_open_layout_s"] = time.perf_counter() - t_load0
        t_extract0 = time.perf_counter()
        try:
            for page_index in range(page_limit):
                page = doc[page_index]
                page_num = page_index + 1
                table_regions, table_tail_blocks = _pdf_plumber_tables_for_plumber_page(
                    plumber_pdf, page_index
                )
                if cache_dicts is not None:
                    d = cache_dicts[page_index]
                else:
                    d = page.get_text("dict")
                # Per-page state: PDF y is page-local; carrying last_y across pages can
                # merge unrelated lines when coordinates happen to align.
                line_buffer: list[tuple[float, str]] = []
                last_y: float | None = None

                for block in d.get("blocks", []):
                    if block.get("type") != 0:
                        continue
                    for line in block.get("lines", []):
                        span_texts: list[str] = []
                        max_size = 0.0
                        bbox = line.get("bbox") or (0, 0, 0, 0)
                        for sp in line.get("spans", []):
                            t = (sp.get("text") or "").strip()
                            if not t:
                                continue
                            span_texts.append(t)
                            max_size = max(max_size, float(sp.get("size") or 0))
                        text = " ".join(span_texts).strip()
                        if not text:
                            continue
                        y = float(bbox[1])
                        if _inside_any_table(bbox, table_regions):
                            continue
                        if last_y is not None and abs(y - last_y) > 14:
                            blocks.extend(
                                _flush_pdf_line_buffer(line_buffer, body_font, page_num)
                            )
                            line_buffer = []
                        last_y = y
                        line_buffer.append((max_size, text))

                blocks.extend(_flush_pdf_line_buffer(line_buffer, body_font, page_num))
                for tb in table_tail_blocks:
                    blocks.append(tb.model_copy(update={"source_page": page_num}))
        finally:
            if plumber_pdf is not None:
                plumber_pdf.close()
    finally:
        doc.close()
    # Run de-duplication before merging so page headers/footers are removed while
    # still isolated lines; then merge and run one more pass for safety.
    deduped_pre_merge = _dedupe_repeated_pdf_fragments(blocks, page_count=page_limit)
    merged = _merge_short_paragraphs(deduped_pre_merge)
    deduped = _dedupe_repeated_pdf_fragments(merged, page_count=page_limit)
    if timings is not None:
        timings["pdf_text_extract_and_structure_s"] = time.perf_counter() - t_extract0
    return deduped


def _estimate_body_fontsize(doc: fitz.Document) -> float:
    sizes: list[float] = []
    for page in doc:
        d = page.get_text("dict")
        for block in d.get("blocks", []):
            if block.get("type") != 0:
                continue
            for line in block.get("lines", []):
                for sp in line.get("spans", []):
                    sz = float(sp.get("size") or 0)
                    if sz > 0:
                        sizes.append(sz)
    if not sizes:
        return 11.0
    sizes.sort()
    return sizes[len(sizes) // 2]


def _flush_pdf_line_buffer(
    line_buffer: list[tuple[float, str]],
    body_font: float,
    source_page: int,
) -> list[ContentBlock]:
    if not line_buffer:
        return []
    out: list[ContentBlock] = []
    lines = [t for _, t in line_buffer]
    text = " ".join(lines).strip()
    if not text:
        return []
    max_sz = max(sz for sz, _ in line_buffer)
    if max_sz >= body_font + 1.5:
        level = 1 if max_sz >= body_font + 4 else 2
        out.append(
            ContentBlock(
                type=BlockType.HEADING,
                text=text,
                level=level,
                source_page=source_page,
            )
        )
    elif _looks_like_list(text):
        ordered = bool(re.match(r"^\d+[\.)]\s+", text.lstrip()))
        out.append(
            ContentBlock(
                type=BlockType.LIST,
                text=text,
                source_page=source_page,
                list_kind="ordered" if ordered else "bullet",
            )
        )
    else:
        out.append(
            ContentBlock(type=BlockType.PARAGRAPH, text=text, source_page=source_page)
        )
    return out


def _looks_like_list(text: str) -> bool:
    t = text.lstrip()
    import re

    return bool(re.match(r"^[-*•]\s+", t) or re.match(r"^\d+[\.)]\s+", t))


def _inside_any_table(bbox: tuple, regions: list[tuple[float, float, float, float]]) -> bool:
    if not regions:
        return False
    x0, y0, x1, y1 = bbox
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    for rx0, ry0, rx1, ry1 in regions:
        if rx0 <= cx <= rx1 and ry0 <= cy <= ry1:
            return True
    return False


def _open_pdfplumber_once(path: Path):
    """Single open for the whole document (avoids 300+ opens on large PDFs)."""
    try:
        import pdfplumber
    except ImportError:
        return None
    try:
        return pdfplumber.open(path)
    except Exception:
        return None


def _pdf_plumber_tables_for_plumber_page(
    plumber_pdf,
    page_index: int,
) -> tuple[list[tuple[float, float, float, float]], list[ContentBlock]]:
    """Table regions + blocks for one page using an already-open pdfplumber PDF."""
    if plumber_pdf is None:
        return [], []
    regions: list[tuple[float, float, float, float]] = []
    table_blocks: list[ContentBlock] = []
    if page_index >= len(plumber_pdf.pages):
        return regions, table_blocks
    page = plumber_pdf.pages[page_index]
    for table in page.find_tables() or []:
        b = table.bbox
        if b:
            regions.append((float(b[0]), float(b[1]), float(b[2]), float(b[3])))
    for table in page.extract_tables() or []:
        if not table:
            continue
        rows = [[(c or "").strip() for c in row] for row in table]
        rows = [r for r in rows if any(cell for cell in r)]
        if rows:
            table_blocks.append(ContentBlock(type=BlockType.TABLE, data=rows))
    return regions, table_blocks


def _merge_short_paragraphs(blocks: list[ContentBlock], max_gap: int = 2) -> list[ContentBlock]:
    merged: list[ContentBlock] = []
    i = 0
    while i < len(blocks):
        b = blocks[i]
        if b.type != BlockType.PARAGRAPH or not b.text:
            merged.append(b)
            i += 1
            continue
        parts = [b.text]
        j = i + 1
        while j < len(blocks) and j - i <= max_gap:
            n = blocks[j]
            if n.type != BlockType.PARAGRAPH or not n.text:
                break
            if b.source_page != n.source_page:
                break
            parts.append(n.text)
            j += 1
        if j - i > 1:
            merged.append(
                ContentBlock(
                    type=BlockType.PARAGRAPH,
                    text=" ".join(parts),
                    source_page=b.source_page,
                )
            )
            i = j
        else:
            merged.append(b)
            i += 1
    return merged


def _normalized_fragment_key(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def _word_count(text: str) -> int:
    return len(re.findall(r"\S+", text or ""))


def _max_consecutive_page_run(pages: set[int]) -> int:
    if not pages:
        return 0
    seq = sorted(pages)
    best = 1
    cur = 1
    for i in range(1, len(seq)):
        if seq[i] == seq[i - 1] + 1:
            cur += 1
            if cur > best:
                best = cur
        else:
            cur = 1
    return best


def _looks_like_page_number_fragment(text: str) -> bool:
    t = (text or "").strip()
    if not t:
        return False
    # Common page-mark patterns from PDF extraction: "12", "- 12 -", "page 12"
    return bool(
        re.match(r"^\W*\d{1,4}\W*$", t, flags=re.IGNORECASE)
        or re.match(r"^page\s+\d{1,4}\W*$", t, flags=re.IGNORECASE)
    )


def _dedupe_repeated_pdf_fragments(
    blocks: list[ContentBlock],
    *,
    page_count: int,
) -> list[ContentBlock]:
    """Drop obvious repeated header/footer-like fragments from PDF extraction.

    Conservative rules:
    - only short heading/paragraph/list blocks,
    - repeated on many distinct pages,
    - never touch tables,
    - remove standalone page-number fragments.
    """
    if not blocks or page_count < 3:
        return blocks

    seen_pages: dict[str, set[int]] = {}
    seen_count: dict[str, int] = {}
    for b in blocks:
        if b.type not in (BlockType.HEADING, BlockType.PARAGRAPH, BlockType.LIST):
            continue
        if not b.text or b.source_page is None:
            continue
        txt = b.text.strip()
        if _looks_like_page_number_fragment(txt):
            continue
        wc = _word_count(txt)
        if wc < 1 or wc > 14 or len(txt) > 120:
            continue
        key = _normalized_fragment_key(txt)
        if not key:
            continue
        seen_pages.setdefault(key, set()).add(int(b.source_page))
        seen_count[key] = seen_count.get(key, 0) + 1

    min_pages = max(6, int(page_count * 0.20))
    min_run_pages = 5
    repeated_keys = {
        key
        for key, pages in seen_pages.items()
        if (
            (len(pages) >= min_pages and seen_count.get(key, 0) >= len(pages))
            or _max_consecutive_page_run(pages) >= min_run_pages
        )
    }
    if not repeated_keys:
        # Still strip obvious page-number-only fragments.
        return [
            b
            for b in blocks
            if not (b.text and _looks_like_page_number_fragment(b.text))
        ]

    out: list[ContentBlock] = []
    for b in blocks:
        txt = (b.text or "").strip()
        if txt and _looks_like_page_number_fragment(txt):
            continue
        if (
            b.type in (BlockType.HEADING, BlockType.PARAGRAPH, BlockType.LIST)
            and txt
            and b.source_page is not None
            and _normalized_fragment_key(txt) in repeated_keys
        ):
            continue
        out.append(b)
    return out
