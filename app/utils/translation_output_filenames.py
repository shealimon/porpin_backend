"""Build download / export filenames: ``{original_stem}-{language}.{ext}``."""

from __future__ import annotations

import re
from pathlib import Path

# Product currently translates to Hinglish only; keep a single label for filenames.
OUTPUT_LANGUAGE_HINGLISH = "Hinglish"

_INVALID_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_MAX_STEM_LEN = 180


def safe_upload_stem(filename: str) -> str:
    """Strip extension, remove characters unsafe in Windows filenames, trim."""
    stem = Path(filename).stem
    stem = _INVALID_FILENAME_CHARS.sub("_", stem)
    stem = stem.strip(" .")
    if not stem:
        stem = "document"
    if len(stem) > _MAX_STEM_LEN:
        stem = stem[:_MAX_STEM_LEN].rstrip(" .")
    return stem or "document"


def translation_output_filename(
    input_filename: str,
    extension: str,
    *,
    output_language: str = OUTPUT_LANGUAGE_HINGLISH,
) -> str:
    """e.g. ``Sleep_Book.pdf`` + ``docx`` → ``Sleep_Book-Hinglish.docx``."""
    ext = extension.lstrip(".").lower()
    base = safe_upload_stem(input_filename)
    return f"{base}-{output_language}.{ext}"
