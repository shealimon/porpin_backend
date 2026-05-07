"""Pydantic models aligned with the frontend `documentModel` / `sequencedModel` / `contentBlocks`."""

from __future__ import annotations

from typing import Annotated, Literal, Union

from pydantic import BaseModel, Field, model_validator

DEFAULT_DOCUMENT_TEMPLATE: Literal["report"] = "report"
DocumentTemplateType = Literal[
    "ebook",
    "report",
    "minimal",
    "blog",
    "academic",
    "bilingual",
]

_ALL_TEMPLATE_TYPES: frozenset[str] = frozenset(
    {"ebook", "report", "minimal", "blog", "academic", "bilingual"}
)


def is_document_template_type(value: str) -> bool:
    return value in _ALL_TEMPLATE_TYPES


class DocumentListBlockModel(BaseModel):
    items: list[str] = Field(default_factory=list)
    ordered: bool = False


class DocumentHeadingModel(BaseModel):
    text: str
    level: int = Field(default=2, ge=2, le=6)
    # Optional stable anchor id for PDF/HTML cross-references (e.g. Table of Contents links).
    anchor: str | None = None


class DocumentChapterModel(BaseModel):
    title: str | None = None
    headings: list[DocumentHeadingModel] = Field(default_factory=list)
    paragraphs: list[str] = Field(default_factory=list)
    lists: list[DocumentListBlockModel] = Field(default_factory=list)


class BlockHeadingModel(BaseModel):
    type: Literal["heading"] = "heading"
    text: str
    level: int | None = Field(default=None, ge=2, le=6)
    chapter_start: bool = False
    is_subheading: bool = False
    milestone_section: bool = False
    # Optional stable anchor id for PDF/HTML cross-references (e.g. Table of Contents links).
    anchor: str | None = None


class BlockParagraphModel(BaseModel):
    type: Literal["paragraph"] = "paragraph"
    text: str
    is_quote: bool = False


class BlockListModel(BaseModel):
    type: Literal["list"] = "list"
    items: list[str] = Field(default_factory=list)
    ordered: bool = False


SequencedBlockModel = Annotated[
    Union[BlockHeadingModel, BlockParagraphModel, BlockListModel],
    Field(discriminator="type"),
]


class DocumentForTemplate(BaseModel):
    """Same shapes as the frontend: chaptered, sequenced (blocks), or flat (headings/paragraphs/lists)."""

    title: str
    subtitle: str | None = None
    chapters: list[DocumentChapterModel] | None = None
    blocks: list[SequencedBlockModel] | None = None
    headings: list[DocumentHeadingModel] | None = None
    paragraphs: list[str] | None = None
    lists: list[DocumentListBlockModel] | None = None

    @model_validator(mode="after")
    def _at_least_one_body_shape(self) -> DocumentForTemplate:
        if self.chapters is not None:
            return self
        if self.blocks is not None:
            return self
        if (
            self.headings is not None
            or self.paragraphs is not None
            or self.lists is not None
        ):
            return self
        raise ValueError(
            "Document must include one of: chapters, blocks, or flat fields "
            "(headings / paragraphs / lists).",
        )
