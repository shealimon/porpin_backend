"""Universal, style-free document tree for post-translation template rendering."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

ContentTag = Literal["body", "toc"]

HeadingSemanticKind = Literal["chapter", "heading", "subheading"]


class StructuredHeading(BaseModel):
    """Document heading; ``level`` 1 is the primary outline level, larger numbers nest deeper."""

    type: Literal["heading"] = "heading"
    level: int = Field(ge=1, le=9)
    text: str
    content_tag: ContentTag = "body"
    kind: HeadingSemanticKind | None = Field(
        default=None,
        description="chapter | heading | subheading — from structure detection before translation.",
    )


class StructuredParagraph(BaseModel):
    type: Literal["paragraph"] = "paragraph"
    text: str
    content_tag: ContentTag = "body"
    is_quote: bool = Field(
        default=False,
        description="Blockquote-style rhythm in PDF/HTML when True.",
    )


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
    subtitle: str | None = None
    authors: list[str] = Field(default_factory=list)
    document_type: str | None = Field(
        default=None,
        description="Coarse genre from pre-translate inference (book, article, educational, …).",
    )
    content: list[StructuredBlock] = Field(default_factory=list)
