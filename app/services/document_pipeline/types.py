"""Shared types for the document pipeline (stage boundaries, no I/O)."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from app.models.document_models import ClassifiedBlock, ContentBlock
from app.models.structured_document import StructuredDocument
from app.services.translation_plan import BlockWork


@dataclass
class ExtractionResult:
    """Output of the text-extraction stage (raw logical blocks from PDF/DOCX/EPUB/TXT/image)."""

    blocks: list[ContentBlock]
    source_path: Path | None = None


@dataclass
class TranslationStageState:
    """Work units for the formatter and optional structured export."""

    block_work: list[BlockWork]
    global_job_count: int


@dataclass
class PostTranslationBundle:
    """Classified, translated content plus structured form for templating and sidecars."""

    classified: list[ClassifiedBlock]
    structured: StructuredDocument
    block_work: list[BlockWork]
    global_job_count: int = 0
    selected_template: str = "report"


@dataclass
class DocumentPipelineResult:
    """Result of a full translate-and-export run (default: DOCX on disk)."""

    output_docx: Path
    post_translation: PostTranslationBundle
    structured_json_path: Path | None = None
    timings_note: dict[str, float] = field(default_factory=dict)
