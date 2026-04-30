"""Universal, style-free document tree for post-translation template rendering."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

ContentTag = Literal["body", "toc"]


class StructuredHeading(BaseModel):
    """Document heading; ``level`` 1 is the primary outline level, larger numbers nest deeper."""

    type: Literal["heading"] = "heading"
    level: int = Field(ge=1, le=9)
    text: str
    content_tag: ContentTag = "body"


class StructuredParagraph(BaseModel):
    type: Literal["paragraph"] = "paragraph"
    text: str
    content_tag: ContentTag = "body"


class StructuredList(BaseModel):
    type: Literal["list"] = "list"
    ordered: bool
    items: list[str]
    content_tag: ContentTag = "body"


class StructuredTable(BaseModel):
    type: Literal["table"] = "table"
    rows: list[list[str]]
    content_tag: ContentTag = "body"


StructuredBlock = (
    StructuredHeading | StructuredParagraph | StructuredList | StructuredTable
)


class StructuredDocument(BaseModel):
    """
    Normalized representation: no fonts, colors, or layout — only hierarchy and plain text.
    ``title`` / ``authors`` are lifted from detected front-matter; body order is in ``content``.
    """

    schema_version: int = 1
    title: str | None = None
    authors: list[str] = Field(default_factory=list)
    content: list[StructuredBlock] = Field(default_factory=list)
