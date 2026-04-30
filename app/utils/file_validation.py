"""Upload validation: allowed types, size, corruption checks, unique safe names."""

from __future__ import annotations

import hashlib
import io
import zipfile
from pathlib import Path

import fitz
from docx import Document
from fastapi import HTTPException, UploadFile

_ALLOWED_SUFFIXES = frozenset(
    {
        ".pdf",
        ".docx",
        ".epub",
        ".ebup",
        ".txt",
        ".png",
        ".jpg",
        ".jpeg",
        ".webp",
        ".tiff",
        ".tif",
        ".gif",
        ".bmp",
    }
)


def assert_allowed_filename(filename: str | None) -> str:
    if not filename:
        raise HTTPException(status_code=400, detail="Filename required.")
    suffix = Path(filename).suffix.lower()
    if suffix not in _ALLOWED_SUFFIXES:
        raise HTTPException(
            status_code=415,
            detail="Supported uploads: PDF, DOCX, EPUB, TXT, and common image formats (PNG, JPEG, WebP, TIFF, etc.).",
        )
    return suffix


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def validate_file_bytes(suffix: str, data: bytes, *, max_bytes: int) -> None:
    if not data:
        raise HTTPException(status_code=400, detail="Empty file.")
    if max_bytes > 0 and len(data) > max_bytes:
        raise HTTPException(
            status_code=413,
            detail=f"File too large (max {max_bytes // (1024 * 1024)}MB).",
        )
    if suffix == ".pdf":
        _validate_pdf(data)
    elif suffix == ".docx":
        _validate_docx(data)
    elif suffix in {".epub", ".ebup"}:
        _validate_epub(data)
    elif suffix == ".txt":
        _validate_txt(data)
    elif suffix in {".png", ".jpg", ".jpeg", ".webp", ".tiff", ".tif", ".gif", ".bmp"}:
        _validate_image_raster(data)


def _validate_pdf(data: bytes) -> None:
    try:
        doc = fitz.open(stream=data, filetype="pdf")
        try:
            if len(doc) < 1:
                raise ValueError("empty pdf")
        finally:
            doc.close()
    except Exception as e:
        raise HTTPException(status_code=400, detail="Corrupted or invalid PDF.") from e


def _validate_docx(data: bytes) -> None:
    try:
        if not zipfile.is_zipfile(io.BytesIO(data)):
            raise ValueError("not a zip")
        Document(io.BytesIO(data))
    except Exception as e:
        raise HTTPException(status_code=400, detail="Corrupted or invalid DOCX.") from e


def _validate_epub(data: bytes) -> None:
    try:
        if not zipfile.is_zipfile(io.BytesIO(data)):
            raise ValueError("not a zip")
        zf = zipfile.ZipFile(io.BytesIO(data))
        with zf:
            names = set(zf.namelist())
            if "META-INF/container.xml" not in names:
                raise ValueError("missing container")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail="Corrupted or invalid EPUB.") from e


def _validate_txt(data: bytes) -> None:
    try:
        data.decode("utf-8")
    except UnicodeDecodeError as e:
        raise HTTPException(
            status_code=400,
            detail="TXT must be valid UTF-8.",
        ) from e


def _validate_image_raster(data: bytes) -> None:
    try:
        from PIL import Image
    except ImportError as e:
        raise HTTPException(
            status_code=503,
            detail="Image uploads require Pillow on the server.",
        ) from e
    try:
        with Image.open(io.BytesIO(data)) as im:
            im.verify()
    except Exception as e:
        raise HTTPException(status_code=400, detail="Corrupted or invalid image file.") from e


async def read_upload_bytes(file: UploadFile) -> bytes:
    try:
        data = await file.read()
    finally:
        await file.close()
    return data
