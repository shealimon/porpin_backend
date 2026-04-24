"""Bundle DOCX/PDF artifacts into a single ZIP on disk."""

from __future__ import annotations

import zipfile
from pathlib import Path


def write_translation_zip(
    members: dict[str, Path],
    zip_path: Path,
) -> Path:
    """
    Create a zip at zip_path containing files keyed by archive name.

    ``members`` maps archive member path (e.g. ``MyBook-Hinglish.docx``) to source file path.
    """
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(
        zip_path,
        "w",
        compression=zipfile.ZIP_DEFLATED,
    ) as zf:
        for arcname, src in members.items():
            if not src.is_file():
                raise FileNotFoundError(f"Missing file for zip member {arcname}: {src}")
            zf.write(src, arcname=arcname)
    return zip_path.resolve()
