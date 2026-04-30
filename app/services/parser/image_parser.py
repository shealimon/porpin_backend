"""Raster image → plain text (OCR). Requires `Pillow` and `pytesseract` + a Tesseract install."""

from __future__ import annotations

import logging
import time
from pathlib import Path

from app.models.document_models import BlockType, ContentBlock

logger = logging.getLogger(__name__)

_IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".webp", ".tiff", ".tif", ".gif", ".bmp"})


def is_image_path(path: Path) -> bool:
    return path.suffix.lower() in _IMAGE_SUFFIXES


def parse_image(path: Path, *, timings: dict[str, float] | None = None) -> list[ContentBlock]:
    t0 = time.perf_counter()
    try:
        from PIL import Image
    except ImportError as e:
        raise RuntimeError(
            "Image text extraction needs Pillow. Install: pip install pillow",
        ) from e
    try:
        import pytesseract
    except ImportError as e:
        raise RuntimeError(
            "Image OCR needs pytesseract. Install: pip install pytesseract "
            "and the Tesseract OCR engine (put `tesseract` on PATH or set TESSDATA_PREFIX).",
        ) from e

    with Image.open(path) as im:
        text = pytesseract.image_to_string(im) or ""

    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not lines:
        blocks = [
            ContentBlock(
                type=BlockType.PARAGRAPH,
                text="(No text recognized in image. Try a clearer scan or a different file.)",
            )
        ]
    else:
        blocks = [ContentBlock(type=BlockType.PARAGRAPH, text=ln) for ln in lines]
    if timings is not None:
        timings["parse_image_ocr_s"] = time.perf_counter() - t0
    logger.info("Image OCR: %d line blocks from %s", len(blocks), path.name)
    return blocks
