"""Stage 2: infer sections / translate vs skip (classification)."""

from __future__ import annotations

import logging

from app.models.document_models import ClassifiedBlock, ContentBlock
from app.services.classifier.section_classifier import classify_blocks

logger = logging.getLogger(__name__)


def classify_source_blocks(blocks: list[ContentBlock]) -> list[ClassifiedBlock]:
    """Turn raw blocks into classified segments for translation planning."""
    logger.info("Classify: %d raw blocks", len(blocks))
    return classify_blocks(blocks)
