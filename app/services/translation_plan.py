"""Build a flat, globally batched translation plan from classified blocks (parse once, translate in bulk)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from app.core.pipeline_settings import get_pipeline_settings
from app.models.document_models import (
    BlockType,
    ClassifiedBlock,
    ContentBlock,
    SectionAction,
    StructuralTag,
)
from app.utils.chunking import chunk_text_respecting_paragraphs, count_tokens
from app.utils.translate_filter import (
    looks_like_sentence_continuation_line,
    looks_like_standalone_section_label,
    plan_paragraph_for_translation,
    sentence_appears_complete,
)


@dataclass(frozen=True)
class ParagraphPiece:
    """One paragraph's translation layout: skip spans and indices into the global job list."""

    structure: tuple[tuple[Literal["skip", "job"], str | int], ...]


@dataclass
class SkipBlockWork:
    block_index: int
    classified: ClassifiedBlock


@dataclass
class TextBlockWork:
    block_index: int
    classified: ClassifiedBlock
    paragraphs: list[ParagraphPiece]


@dataclass
class TableBlockWork:
    block_index: int
    classified: ClassifiedBlock
    # For each cell: None if empty, else paragraph pieces (like multi-para cells)
    cells: list[list[list[ParagraphPiece] | None]]


BlockWork = SkipBlockWork | TextBlockWork | TableBlockWork


def _paragraph_merge_candidate(cb: ClassifiedBlock) -> bool:
    """Body paragraphs eligible for stitching PDF line shards; TOC/title/author unchanged."""
    if cb.action != SectionAction.TRANSLATE:
        return False
    b = cb.block
    if b.type != BlockType.PARAGRAPH:
        return False
    if not (b.text and b.text.strip()):
        return False
    if b.structural_tag in (
        StructuralTag.TITLE,
        StructuralTag.AUTHOR,
        StructuralTag.TOC,
    ):
        return False
    return True


def _should_stop_paragraph_merge(prev_merged_text: str, next_text: str) -> bool:
    """Leave ``next_text`` as its own block when it starts a fresh paragraph."""
    nxt = (next_text or "").strip()
    if looks_like_standalone_section_label(nxt):
        return True
    prev = prev_merged_text.rstrip()
    if not prev:
        return False
    if looks_like_standalone_section_label(prev):
        return True
    if not sentence_appears_complete(prev):
        return False
    return not looks_like_sentence_continuation_line(nxt)


def _glue_merged_paragraph_text(prev: str, nxt: str) -> str:
    """Join two shards of the same logical paragraph (preserve real breaks after full stops)."""
    l = prev.rstrip()
    r = nxt.lstrip()
    if sentence_appears_complete(l):
        if looks_like_sentence_continuation_line(r):
            return f"{l} {r}"
        return f"{l}\n\n{r}"
    return f"{l} {r}"


def merge_adjacent_translate_paragraphs(
    classified: list[ClassifiedBlock],
) -> list[ClassifiedBlock]:
    """Collapse consecutive BODY translate-paragraph shards (typical noisy PDF extraction)."""
    if not classified:
        return classified
    out: list[ClassifiedBlock] = []
    i = 0
    while i < len(classified):
        cb = classified[i]
        if not _paragraph_merge_candidate(cb):
            out.append(cb)
            i += 1
            continue
        merged_text = cb.block.text.strip()
        merged_block_template = cb.block
        j = i + 1
        while j < len(classified):
            nb = classified[j]
            if not _paragraph_merge_candidate(nb):
                break
            ntxt = nb.block.text.strip()
            if _should_stop_paragraph_merge(merged_text, ntxt):
                break
            merged_text = _glue_merged_paragraph_text(merged_text, ntxt)
            j += 1
        out.append(
            ClassifiedBlock(
                block=merged_block_template.model_copy(update={"text": merged_text}),
                action=SectionAction.TRANSLATE,
            )
        )
        i = j
    return out


def _merge_paragraphs_for_translation_units(
    paras: list[str],
    *,
    max_chunk_tokens: int,
) -> list[str]:
    """
    Merge consecutive body paragraphs into fewer planning units.

    Without this, every ``\\n\\n`` paragraph becomes its own plan, producing many
    small segments and slower runs on short multi-paragraph documents.
    """
    if not paras:
        return []
    groups: list[str] = []
    buf: list[str] = []
    buf_tokens = 0
    for p in paras:
        pt = count_tokens(p)
        if pt > max_chunk_tokens:
            if buf:
                groups.append("\n\n".join(buf))
                buf = []
                buf_tokens = 0
            groups.append(p)
            continue
        if buf and buf_tokens + pt > max_chunk_tokens:
            groups.append("\n\n".join(buf))
            buf = []
            buf_tokens = 0
        buf.append(p)
        buf_tokens += pt
    if buf:
        groups.append("\n\n".join(buf))
    return groups


def _paragraph_to_piece(para: str, global_jobs: list[str]) -> ParagraphPiece:
    """Mirror ``_translate_paragraph_with_sentence_filter`` job collection (no API)."""
    settings = get_pipeline_settings()
    plans = plan_paragraph_for_translation(
        para, max_api_word_ratio=settings.translate_api_word_ratio_max
    )
    if not plans:
        return ParagraphPiece((("skip", para),))

    structure: list[tuple[Literal["skip", "job"], str | int]] = []
    buf: list[str] = []

    def flush_buf() -> None:
        nonlocal buf
        if not buf:
            return
        blob = " ".join(buf)
        buf = []
        if not blob.strip():
            return
        if count_tokens(blob) < settings.min_translate_tokens:
            structure.append(("skip", blob))
            return
        chunks = chunk_text_respecting_paragraphs(blob)
        if not chunks:
            structure.append(("skip", blob))
            return
        for ch in chunks:
            structure.append(("job", len(global_jobs)))
            global_jobs.append(ch)

    for p in plans:
        if not p.send_to_api:
            flush_buf()
            structure.append(("skip", p.text))
            continue
        cand = " ".join(buf + [p.text]) if buf else p.text
        if buf and count_tokens(cand) > settings.chunk_max_tokens:
            flush_buf()
        buf.append(p.text)
    flush_buf()

    if not any(kind == "job" for kind, _ in structure):
        joined = " ".join(
            str(v) for kind, v in structure if kind == "skip" and isinstance(v, str)
        )
        return ParagraphPiece((("skip", joined or para),))

    return ParagraphPiece(tuple(structure))


def _text_to_paragraph_pieces(text: str, global_jobs: list[str]) -> list[ParagraphPiece]:
    if not text or not text.strip():
        return []
    t = text.strip()
    paras = [p.strip() for p in re.split(r"\n\s*\n+", t) if p.strip()]
    if not paras:
        paras = [t]
    settings = get_pipeline_settings()
    merged = _merge_paragraphs_for_translation_units(
        paras, max_chunk_tokens=settings.chunk_max_tokens
    )
    return [_paragraph_to_piece(p, global_jobs) for p in merged]


def _reassemble_paragraph(piece: ParagraphPiece, translations: list[str]) -> str:
    parts: list[str] = []
    for kind, val in piece.structure:
        if kind == "skip":
            parts.append(str(val))
        else:
            parts.append(translations[int(val)])
    return " ".join(parts)


def reassemble_from_plan(
    block_work: list[BlockWork],
    translations: list[str],
) -> list[ClassifiedBlock]:
    """Apply flat ``translations`` to the plan and return new classified blocks."""
    out: list[ClassifiedBlock] = []
    for w in block_work:
        if isinstance(w, SkipBlockWork):
            out.append(w.classified)
            continue
        cb = w.classified
        if isinstance(w, TextBlockWork):
            texts = [_reassemble_paragraph(p, translations) for p in w.paragraphs]
            new_text = "\n\n".join(texts)
            b = cb.block
            out.append(
                ClassifiedBlock(
                    block=b.model_copy(update={"text": new_text}),
                    action=cb.action,
                )
            )
            continue
        if isinstance(w, TableBlockWork):
            orig = cb.block.data or []
            new_data: list[list[str]] = []
            for ri, row in enumerate(w.cells):
                new_row: list[str] = []
                for ci, pieces in enumerate(row):
                    orig_cell = (
                        orig[ri][ci]
                        if ri < len(orig) and ci < len(orig[ri])
                        else ""
                    )
                    if pieces is None:
                        new_row.append(orig_cell)
                    else:
                        cell_out = "\n\n".join(
                            _reassemble_paragraph(pp, translations) for pp in pieces
                        )
                        new_row.append(cell_out)
                new_data.append(new_row)
            out.append(
                ClassifiedBlock(
                    block=cb.block.model_copy(update={"data": new_data}),
                    action=cb.action,
                )
            )
    return out


def build_translation_plan(
    classified: list[ClassifiedBlock],
) -> tuple[list[str], list[BlockWork]]:
    """Flatten all API-bound strings into ``global_jobs``; structure stays in ``block_work``."""
    global_jobs: list[str] = []
    block_work: list[BlockWork] = []

    classified = merge_adjacent_translate_paragraphs(classified)

    for i, cb in enumerate(classified):
        if cb.action in (SectionAction.SKIP, SectionAction.OMIT):
            block_work.append(SkipBlockWork(block_index=i, classified=cb))
            continue
        b = cb.block
        if b.type == BlockType.TABLE and b.data:
            grid: list[list[list[ParagraphPiece] | None]] = []
            for row in b.data:
                out_row: list[list[ParagraphPiece] | None] = []
                for cell in row:
                    if cell and str(cell).strip():
                        out_row.append(_text_to_paragraph_pieces(str(cell), global_jobs))
                    else:
                        out_row.append(None)
                grid.append(out_row)
            block_work.append(TableBlockWork(block_index=i, classified=cb, cells=grid))
            continue
        if b.text and b.text.strip():
            paras = _text_to_paragraph_pieces(b.text, global_jobs)
            block_work.append(
                TextBlockWork(block_index=i, classified=cb, paragraphs=paras)
            )
        else:
            block_work.append(SkipBlockWork(block_index=i, classified=cb))

    return global_jobs, block_work