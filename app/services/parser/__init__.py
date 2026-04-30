import time
from pathlib import Path

from app.models.document_models import ContentBlock
from app.services.formatter.book_structure import apply_book_structure_tags
from app.services.parser.docx_parser import parse_docx
from app.services.parser.epub_parser import parse_epub
from app.services.parser.image_parser import is_image_path, parse_image
from app.services.parser.pdf_parser import parse_pdf
from app.services.parser.txt_parser import parse_txt


def parse_document(
    path: Path,
    timings: dict[str, float] | None = None,
    *,
    max_pdf_pages: int | None = None,
    max_preview_words: int | None = None,
) -> list[ContentBlock]:
    suffix = path.suffix.lower()
    t0 = time.perf_counter()
    if suffix == ".pdf":
        blocks = parse_pdf(path, timings=timings, max_pages=max_pdf_pages)
    elif suffix == ".docx":
        blocks = parse_docx(path, timings=timings, max_preview_words=max_preview_words)
    elif suffix in {".epub", ".ebup"}:
        blocks = parse_epub(path)
    elif suffix == ".txt":
        blocks = parse_txt(path, max_preview_words=max_preview_words)
    elif is_image_path(path):
        blocks = parse_image(path, timings=timings)
    else:
        raise ValueError(f"Unsupported document type: {suffix}")
    apply_book_structure_tags(blocks)
    if timings is not None:
        timings["parse_document_total_s"] = time.perf_counter() - t0
    return blocks


__all__ = [
    "is_image_path",
    "parse_document",
    "parse_docx",
    "parse_epub",
    "parse_image",
    "parse_pdf",
    "parse_txt",
]
