"""Structured document blocks produced by parsers and consumed by the pipeline."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field


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
    source_page: int | None = Field(
        default=None,
        description="1-based PDF page index when known; used to slice preview pages.",
    )


class ClassifiedBlock(BaseModel):
    block: ContentBlock
    action: SectionAction
