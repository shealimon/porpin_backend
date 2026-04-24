"""Shared book-style typography constants and text cleanup for export formats."""

from __future__ import annotations

import re
from pathlib import Path

# Display name used in Word / styles (must match embedded font name in fontTable).
LIBRE_BASKERVILLE = "Libre Baskerville"

_FONT_DIR = Path(__file__).resolve().parents[2] / "assets" / "fonts"
LIBRE_BASKERVILLE_REGULAR_TTF = _FONT_DIR / "LibreBaskerville-Regular.ttf"
LIBRE_BASKERVILLE_BOLD_TTF = _FONT_DIR / "LibreBaskerville-Bold.ttf"


def strip_markdown_artifacts(text: str) -> str:
    """Remove common markdown markers so exports stay clean in Word/PDF."""
    if not text:
        return text
    t = text.replace("\r\n", "\n").replace("\r", "\n")
    # ATX headings at line start
    t = re.sub(r"(?m)^#{1,6}\s+", "", t)
    # Bold / italic (non-greedy); run twice for nested remnants
    for _ in range(3):
        nt = re.sub(r"\*\*([^*]+)\*\*", r"\1", t)
        nt = re.sub(r"__([^_]+)__", r"\1", nt)
        nt = re.sub(r"(?<![*])\*(?!\*)([^*]+)\*(?!\*)", r"\1", nt)
        nt = re.sub(r"(?<![_])_(?!_)([^_]+)_(?!_)", r"\1", nt)
        if nt == t:
            break
        t = nt
    t = t.replace("**", "").replace("__", "").replace("`", "")
    # Strip bracketed link targets [text](url) -> text
    t = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", t)
    return t.strip()


def font_files_present() -> bool:
    return LIBRE_BASKERVILLE_REGULAR_TTF.is_file() and LIBRE_BASKERVILLE_BOLD_TTF.is_file()
