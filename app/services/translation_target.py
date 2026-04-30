"""Translation output mode: Roman Hinglish vs Devanagari Hindi."""

from __future__ import annotations

HINGLISH = "hinglish"
HINDI = "hindi"


def normalize_translation_target(raw: str | None) -> str:
    s = (raw or "").strip().lower()
    if s in (HINDI, "hi", "devanagari", "devnagari"):
        return HINDI
    return HINGLISH


def translation_target_label(target: str | None) -> str:
    return "Hindi" if normalize_translation_target(target) == HINDI else "Hinglish"


def download_stem_label(target: str | None) -> str:
    """Filename fragment: ``Book-Hinglish.docx`` / ``Book-Hindi.docx``."""
    return translation_target_label(target)
