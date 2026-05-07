"""PDF → structured blocks using PyMuPDF; tables via pdfplumber when available."""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any

import fitz

from app.core.pipeline_settings import get_pipeline_settings
from app.models.document_models import BlockType, ContentBlock, PdfLineHints
from app.services.parser.pdf_running_header import (
    looks_like_pdf_running_header_line,
    strip_leading_pdf_navigation_crumbs,
)

# Cache page dicts for modest page counts to avoid two get_text("dict") passes per page.
_PDF_TEXTDICT_CACHE_MAX_PAGES = 100

# PyMuPDF font sizes wobble ±~1 pt between spans; avoid treating body lines as headings.
_PDF_HEADING_FONT_DELTA_SUB = 3.25  # min pt above median body → outline level 2
_PDF_HEADING_FONT_DELTA_MAJOR = 7.0  # min pt above median body → outline level 1
# Very long lines are almost never titles; treat as body even if slightly larger.
_PDF_FONT_HEADING_MAX_WORDS = 22


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
                line_buffer: list[tuple[float, str, float, float, float]] = []
                last_y: float | None = None
                last_flush_y1: float | None = None

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
                        bold_strength = _line_bold_strength(line)
                        y0 = float(bbox[1])
                        y1 = float(bbox[3])
                        if _inside_any_table(bbox, table_regions):
                            continue
                        if last_y is not None and abs(y0 - last_y) > 14:
                            gap_before = None
                            if line_buffer and last_flush_y1 is not None:
                                gap_before = min(ln[3] for ln in line_buffer) - last_flush_y1
                            flushed = _flush_pdf_line_buffer(
                                line_buffer,
                                body_font,
                                page_num,
                                gap_before_pt=gap_before,
                            )
                            blocks.extend(flushed)
                            if flushed and flushed[-1].pdf_hints is not None:
                                last_flush_y1 = flushed[-1].pdf_hints.y1
                            line_buffer = []
                        last_y = y0
                        line_buffer.append((max_size, text, bold_strength, y0, y1))

                gap_end = None
                if line_buffer and last_flush_y1 is not None:
                    gap_end = min(ln[3] for ln in line_buffer) - last_flush_y1
                flushed = _flush_pdf_line_buffer(
                    line_buffer,
                    body_font,
                    page_num,
                    gap_before_pt=gap_end,
                )
                blocks.extend(flushed)
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
    # Paragraph merging collapses TOC rows + Preface/Introduction onto one line per page —
    # breaks structure tagging (Preface/Introduction vanish from translated body).
    merged = _merge_short_paragraphs(deduped_pre_merge, max_gap=0)
    deduped = _dedupe_repeated_pdf_fragments(merged, page_count=page_limit)
    no_run_heads = _drop_pdf_running_header_blocks(deduped)
    stitched = _merge_pdf_emphasis_heading_into_prior_paragraph(no_run_heads)
    stitched = _merge_pdf_paragraphs_across_page_breaks(stitched)
    stitched = _strip_leading_nav_crumbs_from_pdf_paragraphs(stitched)
    if timings is not None:
        timings["pdf_text_extract_and_structure_s"] = time.perf_counter() - t_extract0
    return stitched


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


def _word_count_quick(text: str) -> int:
    return len(re.findall(r"\S+", text or ""))


def _span_looks_bold(span: dict[str, Any]) -> bool:
    """Infer bold from font name or MuPDF/PyMuPDF font flags (bit 4 = bold)."""
    font = (span.get("font") or "").lower()
    if any(
        k in font
        for k in (
            "bold",
            "-bd",
            "black",
            "heavy",
            "semibold",
            "demibold",
        )
    ):
        return True
    flags = int(span.get("flags") or 0)
    return bool(flags & 16)


def _line_bold_strength(line: dict[str, Any]) -> float:
    """Share of non-empty span characters that sit in bold-looking spans (0–1)."""
    bold_n = 0
    total_n = 0
    for sp in line.get("spans", []):
        raw = sp.get("text") or ""
        if not raw.strip():
            continue
        total_n += len(raw)
        if _span_looks_bold(sp):
            bold_n += len(raw)
    if total_n <= 0:
        return 0.0
    return bold_n / total_n


def _looks_like_pdf_inline_emphasis_caps_fragment(text: str | None) -> bool:
    """Mid-paragraph shouted line (often extracted as its own PDF row due to font wrap).

    Books typeset emphasized dialogue in all caps ending with ``!`` — not structural headings.
    """
    if not text:
        return False
    t = " ".join(text.split()).strip()
    if len(t) < 6 or len(t) > 130:
        return False
    wc = _word_count_quick(t)
    if wc < 2 or wc > 14:
        return False
    letters = [c for c in t if c.isalpha()]
    if len(letters) < 4:
        return False
    upper_ratio = sum(1 for c in letters if c.isupper()) / len(letters)
    if upper_ratio < 0.88:
        return False
    if t.endswith("!"):
        return True
    if re.search(r"![\"'\u201d\u2019]\s*$", t):
        return True
    return False


def _prior_paragraph_likely_attaches_caps_emphasis(prev_text: str) -> bool:
    """Avoid merging real headings after a clearly finished sentence."""
    t = prev_text.rstrip()
    if len(t) < 12:
        return False
    if t.endswith("."):
        return False
    return True


def _merge_pdf_emphasis_heading_into_prior_paragraph(
    blocks: list[ContentBlock],
) -> list[ContentBlock]:
    """Fold mistaken HEADING shards back into the previous paragraph for export continuity."""
    out: list[ContentBlock] = []
    for b in blocks:
        if (
            b.type == BlockType.HEADING
            and b.text
            and _looks_like_pdf_inline_emphasis_caps_fragment(b.text)
            and out
            and out[-1].type == BlockType.PARAGRAPH
            and (out[-1].text or "").strip()
            and _prior_paragraph_likely_attaches_caps_emphasis(out[-1].text or "")
            and (
                b.source_page is None
                or out[-1].source_page is None
                or b.source_page == out[-1].source_page
            )
        ):
            prev = out[-1]
            merged = (prev.text or "").rstrip() + " " + b.text.strip()
            out[-1] = prev.model_copy(update={"text": merged})
            continue
        out.append(b)
    return out


def _looks_like_pdf_typographic_heading(
    text: str,
    *,
    body_font: float | None,
    max_size: float,
) -> bool:
    """Line(s) typed like body font but visually a section title (common in scanned books).

    PyMuPDF only gives headings when fontsize clearly exceeds ``body_font``; many PDFs keep
    the same point size so "INTRODUCTION" becomes a PARAGRAPH, merges into body → flat PDF/docx.

    Books often set the *first line* of a paragraph in small caps / all caps (same point size).
    Those lines match ``upper_ratio`` but read as prose—comma-heavy, many words, clause endings—
    and must stay PARAGRAPH so translation/PDF export does not split the sentence.
    """
    if body_font is not None and max_size >= body_font + _PDF_HEADING_FONT_DELTA_SUB:
        return False
    t = " ".join((text or "").split()).strip()
    if len(t) < 3 or len(t) > 120:
        return False
    wc = _word_count_quick(t)
    # Real typographic headings are almost always short; long all-caps lines are body openings.
    if wc < 1 or wc > 11:
        return False
    if t.endswith(".") and wc > 5:
        return False
    # Clause punctuation ⇒ continuation of a sentence, not a standalone title.
    if t.endswith(",") or t.endswith(";"):
        return False
    # Internal commas with several words ⇒ prose clause (e.g. "...MOVIES, IN PART BECAUSE...").
    if "," in t and wc >= 6:
        return False
    letters = [c for c in t if c.isalpha()]
    if not letters:
        return False
    upper_ratio = sum(1 for c in letters if c.isupper()) / len(letters)
    if upper_ratio >= 0.88:
        if _looks_like_pdf_inline_emphasis_caps_fragment(t):
            return False
        return True
    return False


def _looks_like_pdf_title_inventory_or_toc_spill(text: str | None) -> bool:
    """Printed TOC / nav spine merged into one line — not a real section heading."""
    if not text:
        return False
    t = " ".join(text.split()).strip()
    if len(t) < 60:
        return False
    q = t.count("?")
    if q >= 3:
        return True
    wc = _word_count_quick(t)
    if len(t) >= 100 and q >= 2 and wc >= 35:
        return True
    ch = len(re.findall(r"(?i)\bchapter\s+\d+", t))
    if ch >= 2 and wc >= 25:
        return True
    return False


def _flush_pdf_line_buffer(
    line_buffer: list[tuple[float, str, float, float, float]],
    body_font: float,
    source_page: int,
    *,
    gap_before_pt: float | None = None,
) -> list[ContentBlock]:
    if not line_buffer:
        return []
    out: list[ContentBlock] = []
    lines = [t for _, t, _, _, _ in line_buffer]
    text = " ".join(lines).strip()
    if not text:
        return []
    max_sz = max(sz for sz, _, _, _, _ in line_buffer)
    bold_strengths = [br for _, _, br, _, _ in line_buffer]
    bold_max = max(bold_strengths) if bold_strengths else 0.0
    bold_avg = sum(bold_strengths) / len(bold_strengths) if bold_strengths else 0.0
    bold_signal = max(bold_max, bold_avg)
    min_y0 = min(ln[3] for ln in line_buffer)
    max_y1 = max(ln[4] for ln in line_buffer)
    hints = PdfLineHints(
        font_pt_max=max_sz,
        bold_fraction=bold_signal,
        lines_merged=len(line_buffer),
        y0=min_y0,
        y1=max_y1,
        gap_before_pt=gap_before_pt,
        body_font_pt=body_font,
    )
    wc_line = _word_count_quick(text)
    emph_caps = _looks_like_pdf_inline_emphasis_caps_fragment(text)
    if _looks_like_pdf_title_inventory_or_toc_spill(text):
        out.append(
            ContentBlock(
                type=BlockType.PARAGRAPH,
                text=text,
                source_page=source_page,
                pdf_hints=hints,
            )
        )
    elif (
        not emph_caps
        and max_sz >= body_font + _PDF_HEADING_FONT_DELTA_SUB
        and wc_line <= _PDF_FONT_HEADING_MAX_WORDS
    ):
        level = (
            1 if max_sz >= body_font + _PDF_HEADING_FONT_DELTA_MAJOR else 2
        )
        out.append(
            ContentBlock(
                type=BlockType.HEADING,
                text=text,
                level=level,
                source_page=source_page,
                pdf_hints=hints,
            )
        )
    elif not emph_caps and _looks_like_pdf_typographic_heading(
        text, body_font=body_font, max_size=max_sz
    ):
        out.append(
            ContentBlock(
                type=BlockType.HEADING,
                text=text,
                level=2,
                source_page=source_page,
                pdf_hints=hints,
            )
        )
    elif (
        not emph_caps
        and not _looks_like_list(text)
        and len(line_buffer) <= 2
        and wc_line <= 11
        and len(text) <= 160
        and max_sz < body_font + _PDF_HEADING_FONT_DELTA_SUB
        and bold_signal >= 0.88
        and not (text.rstrip().endswith((".", "!", "?")) and wc_line > 9)
        and not ("," in text and wc_line >= 8)
    ):
        # Same point size as body but strongly bold — only for short runs (not TOC dumps).
        level = 2 if wc_line <= 6 else 3
        out.append(
            ContentBlock(
                type=BlockType.HEADING,
                text=text,
                level=level,
                source_page=source_page,
                pdf_hints=hints,
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
                pdf_hints=hints,
            )
        )
    else:
        out.append(
            ContentBlock(
                type=BlockType.PARAGRAPH,
                text=text,
                source_page=source_page,
                pdf_hints=hints,
            )
        )
    return out


def _looks_like_list(text: str) -> bool:
    t = text.lstrip()
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


# Sentence-ending punctuation before optional closing quotes/brackets (PDF → prose).
_PDF_SENTENCE_COMPLETE_END = re.compile(
    r"""[.!?…।]['"\u201d\u2019)\]]*\s*$""",
    flags=re.UNICODE,
)


def _pdf_text_looks_like_sentence_complete(text: str) -> bool:
    """True when extracted text likely ends a sentence — do not glue next page."""
    t = (text or "").strip()
    if not t:
        return True
    if t.endswith("..."):
        return True
    return bool(_PDF_SENTENCE_COMPLETE_END.search(t))


def _looks_like_pdf_toc_leader_or_leaf(text: str) -> bool:
    """TOC rows (dot leaders + leaf) must not be merged onto the next page's body."""
    t = " ".join((text or "").split()).strip()
    if not t or len(t) > 160:
        return False
    if re.search(r"\.{3,}", t) and re.search(
        r"(?:\d{1,4}|[ivxlcdm]{1,10})\s*$", t, flags=re.IGNORECASE
    ):
        return True
    if "..." in t and re.search(
        r"(?:\d{1,4}|[ivxlcdm]{1,10})\s*$", t, flags=re.IGNORECASE
    ):
        return True
    return False


def _merge_pdf_paragraphs_across_page_breaks(blocks: list[ContentBlock]) -> list[ContentBlock]:
    """Join paragraphs split only by a page break (common in two-up / reflow PDFs).

    PyMuPDF yields one paragraph block per page column; mid-sentence page
    boundaries become separate ``ContentBlock``s and then separate PDF/DOCX paragraphs
    after translation. Glue when the prior text clearly does not end a sentence.
    """
    if not blocks:
        return blocks
    out: list[ContentBlock] = []
    for b in blocks:
        if (
            out
            and out[-1].type == BlockType.PARAGRAPH
            and b.type == BlockType.PARAGRAPH
            and (out[-1].text or "").strip()
            and (b.text or "").strip()
            and out[-1].source_page is not None
            and b.source_page is not None
            and int(b.source_page) == int(out[-1].source_page) + 1
            and not _pdf_text_looks_like_sentence_complete((out[-1].text or "").strip())
            and not _looks_like_pdf_toc_leader_or_leaf((out[-1].text or "").strip())
            and not _looks_like_pdf_toc_leader_or_leaf((b.text or "").strip())
        ):
            prev = out[-1]
            ptxt = (prev.text or "").rstrip()
            ntxt = (b.text or "").lstrip()
            if ptxt.endswith("-"):
                merged_text = ptxt[:-1].rstrip() + ntxt
            else:
                merged_text = ptxt + " " + ntxt
            merged_text = " ".join(merged_text.split())
            out[-1] = prev.model_copy(
                update={
                    "text": merged_text,
                    "source_page": b.source_page,
                    "pdf_hints": None,
                }
            )
        else:
            out.append(b)
    return out


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
                    pdf_hints=b.pdf_hints,
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


def _drop_pdf_running_header_blocks(blocks: list[ContentBlock]) -> list[ContentBlock]:
    out: list[ContentBlock] = []
    for b in blocks:
        if b.type in (BlockType.PARAGRAPH, BlockType.LIST, BlockType.HEADING):
            if b.text and looks_like_pdf_running_header_line(b.text):
                continue
        out.append(b)
    return out


def _strip_leading_nav_crumbs_from_pdf_paragraphs(blocks: list[ContentBlock]) -> list[ContentBlock]:
    """Remove spine-side nav labels glued to the first sentence (``Notes Index Preface …``)."""
    out: list[ContentBlock] = []
    for b in blocks:
        if b.type != BlockType.PARAGRAPH or not b.text:
            out.append(b)
            continue
        stripped = strip_leading_pdf_navigation_crumbs(b.text)
        if not stripped.strip():
            out.append(b)
            continue
        if stripped == b.text:
            out.append(b)
        else:
            out.append(b.model_copy(update={"text": stripped}))
    return out


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
