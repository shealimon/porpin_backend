"""Structured document blocks produced by parsers and consumed by the pipeline."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field

ListKind = Literal["bullet", "ordered"]


class PdfLineHints(BaseModel):
    """Optional PDF text-run geometry from PyMuPDF, for multi-signal heading detection."""

    font_pt_max: float = Field(default=0.0, description="Largest span font size in the merged run")
    bold_fraction: float = Field(
        default=0.0, ge=0.0, le=1.0, description="Share of characters in bold-looking spans"
    )
    lines_merged: int = Field(default=1, ge=1, description="Source lines merged into this block")
    y0: float = Field(default=0.0, description="Top of bbox in PDF page coordinates")
    y1: float = Field(default=0.0, description="Bottom of bbox in PDF page coordinates")
    gap_before_pt: float | None = Field(
        default=None,
        description="Vertical gap from the previous text block on the same page (points)",
    )
    body_font_pt: float | None = Field(
        default=None, description="Median body font size for the document at extraction time"
    )


class BlockType(StrEnum):
    HEADING = "heading"
    PARAGRAPH = "paragraph"
    LIST = "list"
    TABLE = "table"


class StructuralTag(StrEnum):
    """Front-matter / TOC hints for book-style export (body uses ``None``)."""

    TITLE = "title"
    AUTHOR = "author"
    TOC = "toc"


class SectionAction(StrEnum):
    TRANSLATE = "TRANSLATE"
    SKIP = "SKIP"
    OMIT = "OMIT"


class ContentBlock(BaseModel):
    """One logical unit in the source document."""

    type: BlockType
    text: str | None = Field(default=None, description="Plain text for non-table blocks")
    data: list[list[str]] | None = Field(
        default=None,
        description="Table rows; each row is a list of cell strings",
    )
    level: int = Field(default=1, ge=1, le=9, description="Heading level when type is heading")
    structural_tag: StructuralTag | None = Field(
        default=None,
        description="Title page, author line(s), or TOC region; None for main body.",
    )
    list_kind: ListKind | None = Field(
        default=None,
        description="When type is list: bullet vs numbered (from source; optional).",
    )
    source_page: int | None = Field(
        default=None,
        description="1-based PDF page index when known; used to slice preview pages.",
    )
    semantic_role: str | None = Field(
        default=None,
        description=(
            "chapter | heading | subheading | subtitle | quote | … — assigned in the "
            "semantic-enrichment stage before translation so structure survives translate → PDF."
        ),
    )
    pdf_hints: PdfLineHints | None = Field(
        default=None,
        description="Populated for PDF extraction only; feeds structure / heading confidence.",
    )


class ClassifiedBlock(BaseModel):
    block: ContentBlock
    action: SectionAction
