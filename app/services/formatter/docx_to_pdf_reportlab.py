"""DOCX → PDF using ReportLab (no Word/LibreOffice). Book-style Libre Baskerville layout."""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from xml.sax.saxutils import escape

from docx import Document
from docx.table import Table as DocxTable
from docx.text.paragraph import Paragraph as DocxParagraph
from docx.oxml.ns import qn

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_JUSTIFY, TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import inch
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from app.services.formatter.book_typography import (
    LIBRE_BASKERVILLE_BOLD_TTF,
    LIBRE_BASKERVILLE_REGULAR_TTF,
    font_files_present,
    strip_markdown_artifacts,
)
from app.services.formatter.document_builder import (
    STYLE_BOOK_AUTHOR,
    STYLE_BOOK_TITLE,
    STYLE_TOC_ENTRY,
    STYLE_TOC_HEADING,
)

logger = logging.getLogger(__name__)


def _rl_flow_markup(raw: str) -> str:
    t = strip_markdown_artifacts(raw)
    return escape(t).replace("\n", "<br/>")


def _register_fallback_body_font() -> tuple[str, str]:
    """Register a TTF with decent Unicode coverage; return (regular, bold) names."""
    candidates: list[Path] = []
    if sys.platform == "win32":
        windir = os.environ.get("WINDIR", r"C:\Windows")
        candidates.extend(
            [
                Path(windir) / "Fonts" / "arial.ttf",
                Path(windir) / "Fonts" / "calibri.ttf",
            ]
        )
    else:
        candidates.extend(
            [
                Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
                Path("/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf"),
            ]
        )
    for p in candidates:
        if p.is_file():
            try:
                pdfmetrics.registerFont(TTFont("TranslBodyFont", str(p)))
                return "TranslBodyFont", "TranslBodyFont"
            except Exception as e:
                logger.debug("Could not register font %s: %s", p, e)
    return "Helvetica", "Helvetica"


def _register_lb_fonts() -> tuple[str, str]:
    if not font_files_present():
        return _register_fallback_body_font()
    try:
        pdfmetrics.registerFont(
            TTFont("LBRegular", str(LIBRE_BASKERVILLE_REGULAR_TTF))
        )
        pdfmetrics.registerFont(TTFont("LBBold", str(LIBRE_BASKERVILLE_BOLD_TTF)))
        return "LBRegular", "LBBold"
    except Exception as e:
        logger.warning("Libre Baskerville TTF registration failed: %s", e)
        return _register_fallback_body_font()


def _iter_docx_blocks(doc: Document):
    for child in doc.element.body:
        if child.tag == qn("w:p"):
            yield ("p", DocxParagraph(child, doc))
        elif child.tag == qn("w:tbl"):
            yield ("t", DocxTable(child, doc))


def _para_style_name(p: DocxParagraph) -> str:
    try:
        if p.style and p.style.name:
            return str(p.style.name)
    except Exception:
        pass
    return ""


def _left_indent_pt(p: DocxParagraph) -> float:
    try:
        ind = p.paragraph_format.left_indent
        if ind is None:
            return 0.0
        return float(ind.pt)
    except Exception:
        return 0.0


def _space_before_pt(p: DocxParagraph) -> float:
    try:
        sb = p.paragraph_format.space_before
        if sb is None:
            return 0.0
        return float(sb.pt)
    except Exception:
        return 0.0


def _append_book_title_page_to_story(
    story: list,
    title_raw: str,
    authors: list[str],
    usable_width: float,
    usable_height: float,
    extra: dict[str, ParagraphStyle],
    font_regular: str,
    font_bold: str,
) -> None:
    title_xml = _rl_flow_markup(title_raw)
    max_title_h = usable_height * 0.58
    min_title_pt = 24
    chosen: ParagraphStyle | None = None
    title_h = 0.0
    for size in range(60, min_title_pt - 1, -2):
        sk = f"_ttlfit_{size}"
        if sk not in extra:
            extra[sk] = ParagraphStyle(
                name=sk,
                fontName=font_bold,
                fontSize=size,
                leading=size * 1.12,
                alignment=TA_CENTER,
                spaceAfter=6,
            )
        st = extra[sk]
        probe = Paragraph(title_xml, st)
        _w, h = probe.wrap(usable_width, 100000)
        if h <= max_title_h:
            chosen = st
            title_h = h
            break
    if chosen is None:
        sk = f"_ttlfit_{min_title_pt}"
        if sk not in extra:
            extra[sk] = ParagraphStyle(
                name=sk,
                fontName=font_bold,
                fontSize=min_title_pt,
                leading=min_title_pt * 1.12,
                alignment=TA_CENTER,
                spaceAfter=6,
            )
        chosen = extra[sk]
        probe = Paragraph(title_xml, chosen)
        _w, title_h = probe.wrap(usable_width, 100000)

    gap = 24
    author_style = ParagraphStyle(
        name="_authblk",
        fontName=font_regular,
        fontSize=24,
        leading=28.8,
        alignment=TA_CENTER,
        spaceAfter=8,
    )
    author_xmls = [_rl_flow_markup(a) for a in authors if a.strip()]
    author_block = 0.0
    for ax in author_xmls:
        ap = Paragraph(ax, author_style)
        _w, ah = ap.wrap(usable_width, 100000)
        author_block += ah + 4

    content_h = title_h + gap + author_block
    top_pad = max(40.0, (usable_height - content_h) / 2)

    story.append(Spacer(1, top_pad))
    story.append(Paragraph(title_xml, chosen))
    story.append(Spacer(1, gap))
    for ax in author_xmls:
        story.append(Paragraph(ax, author_style))


def _paragraph_style_for_docx_para(
    p: DocxParagraph,
    extra: dict[str, ParagraphStyle],
    font_regular: str,
    font_bold: str,
) -> tuple[ParagraphStyle, str, int | None]:
    raw = p.text or ""
    name = _para_style_name(p)
    if name == STYLE_TOC_HEADING:
        key = "_toc_hd"
        if key not in extra:
            extra[key] = ParagraphStyle(
                name=key,
                fontName=font_bold,
                fontSize=16,
                leading=19,
                alignment=TA_CENTER,
                spaceBefore=6,
                spaceAfter=16,
            )
        return extra[key], _rl_flow_markup(raw), None
    if name == STYLE_TOC_ENTRY:
        li = int(round(_left_indent_pt(p)))
        key = f"_toc_e_{li}"
        if key not in extra:
            extra[key] = ParagraphStyle(
                name=key,
                fontName=font_regular,
                fontSize=12,
                leading=15,
                alignment=TA_LEFT,
                leftIndent=float(li),
                spaceAfter=4,
            )
        return extra[key], _rl_flow_markup(raw), None
    if name.startswith("Heading"):
        try:
            lvl = max(1, min(6, int(name.replace("Heading", "").strip() or "1")))
        except ValueError:
            lvl = 1
        key = f"_hd{lvl}"
        if key not in extra:
            if lvl == 1:
                extra[key] = ParagraphStyle(
                    name=key,
                    fontName=font_bold,
                    fontSize=18,
                    leading=22,
                    alignment=TA_CENTER,
                    spaceBefore=18,
                    spaceAfter=14 + 22,
                )
            else:
                extra[key] = ParagraphStyle(
                    name=key,
                    fontName=font_bold,
                    fontSize=14,
                    leading=18,
                    alignment=TA_LEFT,
                    spaceBefore=10,
                    spaceAfter=8,
                )
        return extra[key], _rl_flow_markup(raw), lvl
    if "_body" not in extra:
        extra["_body"] = ParagraphStyle(
            name="_body",
            fontName=font_regular,
            fontSize=12,
            leading=18,
            alignment=TA_JUSTIFY,
            spaceAfter=8,
        )
    return extra["_body"], _rl_flow_markup(raw), None


def convert_docx_to_pdf_reportlab(docx_path: Path, pdf_path: Path) -> Path:
    """Read ``docx_path`` and write a simple PDF to ``pdf_path``."""
    docx_path = docx_path.resolve()
    pdf_path = pdf_path.resolve()
    pdf_path.parent.mkdir(parents=True, exist_ok=True)

    font_regular, font_bold = _register_lb_fonts()
    doc = Document(str(docx_path))

    pdf_path.unlink(missing_ok=True)
    rl = SimpleDocTemplate(
        str(pdf_path),
        pagesize=A4,
        leftMargin=1 * inch,
        rightMargin=1 * inch,
        topMargin=1 * inch,
        bottomMargin=1 * inch,
        title="Translated document",
    )
    extra_styles: dict[str, ParagraphStyle] = {}
    story: list = []
    usable_width = rl.width
    usable_height = rl.height
    first_main_heading = True

    blocks_list = list(_iter_docx_blocks(doc))
    i = 0
    while i < len(blocks_list):
        kind, block = blocks_list[i]
        if kind == "p":
            p = block
            pname = _para_style_name(p)
            raw = (p.text or "").strip()

            if not raw:
                try:
                    if p.contains_page_break():
                        story.append(PageBreak())
                    else:
                        sb = _space_before_pt(p)
                        if sb > 12:
                            story.append(Spacer(1, sb))
                        else:
                            story.append(Spacer(1, 6))
                except Exception:
                    story.append(Spacer(1, 6))
                i += 1
                continue

            if pname == STYLE_BOOK_TITLE:
                title_raw = raw
                authors: list[str] = []
                j = i + 1
                while j < len(blocks_list):
                    k2, b2 = blocks_list[j]
                    if k2 != "p":
                        break
                    p2 = b2
                    if _para_style_name(p2) == STYLE_BOOK_AUTHOR:
                        t2 = (p2.text or "").strip()
                        if t2 and t2 != "\u00a0":
                            authors.append(t2)
                        j += 1
                        continue
                    break
                while j < len(blocks_list):
                    k2, b2 = blocks_list[j]
                    if k2 == "p":
                        p2 = b2
                        r2 = (p2.text or "").strip()
                        if not r2:
                            try:
                                if p2.contains_page_break():
                                    j += 1
                                    continue
                            except Exception:
                                pass
                    break
                _append_book_title_page_to_story(
                    story,
                    title_raw,
                    authors,
                    usable_width,
                    usable_height,
                    extra_styles,
                    font_regular,
                    font_bold,
                )
                story.append(PageBreak())
                i = j
                continue

            st, xml_text, hlvl = _paragraph_style_for_docx_para(
                p, extra_styles, font_regular, font_bold
            )
            if hlvl == 1:
                if not first_main_heading:
                    story.append(PageBreak())
                first_main_heading = False
            story.append(Paragraph(xml_text, st))
            i += 1
        else:
            tbl = block
            cell_style = ParagraphStyle(
                name="tblcell",
                fontName=font_regular,
                fontSize=12,
                leading=18,
                alignment=TA_JUSTIFY,
            )
            rows_data: list[list[Paragraph]] = []
            for row in tbl.rows:
                cells: list[Paragraph] = []
                for c in row.cells:
                    raw_cell = (c.text or "").strip() or " "
                    cells.append(Paragraph(_rl_flow_markup(raw_cell), cell_style))
                rows_data.append(cells)
            if not rows_data:
                i += 1
                continue
            ncols = max(len(r) for r in rows_data)
            for r in rows_data:
                while len(r) < ncols:
                    r.append(Paragraph(" ", cell_style))
            col_w = usable_width / max(1, ncols)
            rl_tbl = Table(
                rows_data,
                colWidths=[col_w] * ncols,
                repeatRows=1 if len(rows_data) > 1 else 0,
            )
            rl_tbl.setStyle(
                TableStyle(
                    [
                        ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
                        ("VALIGN", (0, 0), (-1, -1), "TOP"),
                        ("LEFTPADDING", (0, 0), (-1, -1), 4),
                        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
                        ("TOPPADDING", (0, 0), (-1, -1), 4),
                        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                    ]
                )
            )
            story.append(rl_tbl)
            story.append(Spacer(1, 12))
            i += 1

    if not story:
        story.append(
            Paragraph(
                "(empty document)",
                ParagraphStyle(name="empty_note", fontName=font_regular, fontSize=12),
            )
        )

    def _draw_page_number(canvas, doc) -> None:
        canvas.saveState()
        canvas.setFont(font_regular, 9)
        pw, _ph = doc.pagesize
        canvas.drawCentredString(pw / 2, 0.65 * inch, str(canvas.getPageNumber()))
        canvas.restoreState()

    rl.build(story, onFirstPage=_draw_page_number, onLaterPages=_draw_page_number)
    if not pdf_path.is_file():
        raise RuntimeError("ReportLab did not produce a PDF file.")
    return pdf_path
