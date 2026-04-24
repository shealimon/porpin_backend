"""Apply translated ClassifiedBlock text onto an existing DOCX (preserves structure)."""

from __future__ import annotations

import logging
from pathlib import Path

from docx import Document
from docx.table import Table
from docx.text.paragraph import Paragraph

from app.models.document_models import BlockType, ClassifiedBlock, SectionAction
from app.services.formatter.book_typography import strip_markdown_artifacts
from app.services.formatter.document_builder import (
    _apply_body_paragraph,
    _apply_list_paragraph,
    _apply_main_heading_paragraph,
    _apply_sub_heading_paragraph,
    _clear_paragraph_runs,
    _configure_document_defaults,
    _style_table_cell,
)
from app.services.formatter.docx_embed_fonts import embed_libre_baskerville

logger = logging.getLogger(__name__)


def apply_translations_inplace(
    source_path: Path,
    classified: list[ClassifiedBlock],
    output_path: Path,
) -> Path:
    """
    Walk the document body in the same order as ``parse_docx`` and apply
    translated text for TRANSLATE blocks; SKIP leaves content unchanged.
    """
    doc = Document(str(source_path))
    _configure_document_defaults(doc)

    idx = 0
    body = doc.element.body
    first_main_heading = True

    for child in body:
        tag = child.tag.split("}")[-1] if "}" in child.tag else child.tag
        if tag == "tbl":
            table = Table(child, doc)
            if idx >= len(classified):
                break
            cb = classified[idx]
            idx += 1
            if cb.block.type != BlockType.TABLE or not cb.block.data:
                continue
            _apply_table(table, cb)
            continue
        if tag == "p":
            para = Paragraph(child, doc)
            if not para.text.strip():
                continue
            if idx >= len(classified):
                break
            cb = classified[idx]
            idx += 1
            if cb.action == SectionAction.OMIT:
                if cb.block.type == BlockType.HEADING and cb.block.level == 1:
                    first_main_heading = False
                _clear_paragraph_runs(para)
                continue
            if cb.action == SectionAction.SKIP:
                if cb.block.type == BlockType.HEADING and cb.block.level == 1:
                    first_main_heading = False
                continue
            new_text = strip_markdown_artifacts(cb.block.text or "")
            bt = cb.block.type
            if bt == BlockType.HEADING:
                lvl = max(1, min(9, cb.block.level))
                if lvl == 1:
                    brk = not first_main_heading
                    first_main_heading = False
                    _apply_main_heading_paragraph(
                        para, new_text, page_break_before=brk
                    )
                else:
                    _apply_sub_heading_paragraph(para, new_text)
            elif bt == BlockType.LIST:
                _apply_list_paragraph(para, new_text)
            else:
                _apply_body_paragraph(para, new_text)

    if idx != len(classified):
        logger.warning(
            "In-place apply: block index %d != classified len %d (structure drift)",
            idx,
            len(classified),
        )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(output_path))
    embed_libre_baskerville(output_path)
    return output_path.resolve()


def _apply_table(table: Table, cb: ClassifiedBlock) -> None:
    data = cb.block.data or []
    if cb.action == SectionAction.SKIP:
        return
    if cb.action == SectionAction.OMIT:
        for row in table.rows:
            for cell in row.cells:
                cell.text = ""
        return
    for ri, row in enumerate(table.rows):
        if ri >= len(data):
            break
        src_row = data[ri]
        for ci, cell in enumerate(row.cells):
            if ci >= len(src_row):
                break
            new_text = src_row[ci]
            _style_table_cell(cell, new_text)
