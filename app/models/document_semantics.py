"""Semantic roles and light-weight document metadata for structure-first translation exports."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

SemanticRole = Literal[
    "document_title",
    "subtitle",
    "author",
    "chapter",
    "heading",
    "subheading",
    "paragraph",
    "quote",
]

DocumentKind = Literal[
    "book",
    "article",
    "report",
    "notes",
    "educational",
    "q_and_a",
    "manual",
    "documentation",
    "general",
]


class DocumentMetadata(BaseModel):
    """Detected before translation; merged into :class:`~app.models.structured_document.StructuredDocument` where applicable."""

    document_type: DocumentKind = "general"
    """Coarse genre / layout family for template selection and future per-type rules."""

    subtitle_block_index: int | None = None
    """Index into the enriched ``classified`` list for the block carrying the subtitle (if any)."""

    detected_chapter_titles: list[str] = Field(default_factory=list)
    """Source-language chapter labels (best-effort), for debugging and future TOC hints."""

