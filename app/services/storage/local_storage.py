"""UUID-based temp file helpers under the OS temp directory."""

from __future__ import annotations

import uuid
from pathlib import Path

from app.core.pipeline_settings import get_pipeline_settings


def save_temp_file(data: bytes, suffix: str) -> Path:
    """Persist bytes to a unique file under temp_dir; returns absolute path."""
    settings = get_pipeline_settings()
    settings.temp_dir.mkdir(parents=True, exist_ok=True)
    name = f"{uuid.uuid4()}{suffix}"
    path = settings.temp_dir / name
    path.write_bytes(data)
    return path.resolve()


def get_file_path(key: str) -> Path:
    """Resolve a basename key under the configured temp directory."""
    return (get_pipeline_settings().temp_dir / key).resolve()


def delete_file(path: Path | str) -> None:
    p = Path(path)
    try:
        if p.is_file():
            p.unlink()
    except OSError:
        pass
