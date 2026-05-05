"""Translation job orchestration: delegates to :mod:`app.services.document_pipeline`."""

from __future__ import annotations

import logging
import shutil
import subprocess
import tempfile
from pathlib import Path

from collections.abc import Callable

from app.core.pipeline_settings import get_pipeline_settings
from app.models.document_models import ClassifiedBlock, ContentBlock
from app.observability.pipeline_performance import PipelinePerfReport
from app.services.document_pipeline.orchestrator import run_translate_export_docx_pipeline
from app.services.document_pipeline.paragraph_overlap_dedupe import (
    dedupe_consecutive_redundant_translate_paragraphs,
)
from app.services.document_pipeline.stages.export_docx import (
    write_classified_to_docx,
    write_structured_sidecar,
)
from app.services.parser import parse_document
from app.services.structured_document_builder import build_structured_document
from app.services.translation_plan import BlockWork, reassemble_from_plan
from app.utils.chunking import count_tokens

logger = logging.getLogger(__name__)


def estimate_input_tokens_from_blocks(blocks: list[ContentBlock]) -> int:
    """Token estimate from parsed blocks (no I/O)."""
    total = 0
    for b in blocks:
        if b.text:
            total += count_tokens(b.text)
        if b.data:
            for row in b.data:
                for cell in row:
                    if cell:
                        total += count_tokens(cell)
    return total


def estimate_input_tokens(path: Path) -> int:
    """Rough input token count for job caps (parse + tiktoken)."""
    return estimate_input_tokens_from_blocks(parse_document(path))


def run_pipeline(
    input_path: Path,
    *,
    blocks: list[ContentBlock] | None = None,
    on_progress: Callable[[int], None] | None = None,
    on_tokens: Callable[[int], None] | None = None,
    progress_job_id: str | None = None,
    perf_report: PipelinePerfReport | None = None,
    template_id: str | None = None,
    translation_target: str = "hinglish",
) -> Path:
    """Parse → classify → translate → structure (sidecar) → DOCX (see ``document_pipeline`` package)."""
    return run_translate_export_docx_pipeline(
        input_path,
        blocks=blocks,
        on_progress=on_progress,
        on_tokens=on_tokens,
        progress_job_id=progress_job_id,
        perf_report=perf_report,
        template_id=template_id,
        translation_target=translation_target,
    )


def write_translated_docx(
    input_path: Path,
    *,
    block_work: list[BlockWork],
    translated_segments: list[str],
    output_docx: Path,
    structured_json_path: Path | None = None,
) -> list[ClassifiedBlock]:
    """Reassemble classified blocks and write DOCX (in-place or rebuild); optional sidecar JSON."""
    translated_classified = reassemble_from_plan(block_work, translated_segments)
    translated_classified = dedupe_consecutive_redundant_translate_paragraphs(
        translated_classified,
    )
    structured = build_structured_document(translated_classified)
    write_classified_to_docx(
        input_path,
        translated_classified,
        output_docx,
        structured=structured,
    )
    if structured_json_path is not None:
        write_structured_sidecar(structured, structured_json_path)
    return translated_classified


def try_convert_docx_to_pdf(docx_path: Path) -> Path:
    """Best-effort DOCX→PDF; raises RuntimeError if no converter available."""
    pdf_path = docx_path.with_suffix(".pdf")

    # 1) LibreOffice — good fidelity when installed; fails fast if not.
    soffice = _find_soffice()
    if soffice:
        tmp = tempfile.mkdtemp(prefix="translator_pdf_")
        try:
            cmd = [
                str(soffice),
                "--headless",
                "--convert-to",
                "pdf",
                "--outdir",
                tmp,
                str(docx_path),
            ]
            subprocess.run(cmd, check=True, capture_output=True, timeout=120)
            produced = Path(tmp) / (docx_path.stem + ".pdf")
            if produced.is_file():
                produced.replace(pdf_path)
                logger.info("DOCX→PDF via LibreOffice → %s", pdf_path)
                return pdf_path.resolve()
        except Exception as e:
            logger.debug("LibreOffice convert failed: %s", e)

    # 2) ReportLab — pure Python; works without Word (docx2pdf can hang if Word is missing).
    try:
        from app.services.formatter.docx_to_pdf_reportlab import (
            convert_docx_to_pdf_reportlab,
        )

        convert_docx_to_pdf_reportlab(docx_path, pdf_path)
        if pdf_path.is_file():
            logger.info("DOCX->PDF via ReportLab -> %s", pdf_path)
            return pdf_path.resolve()
    except Exception as e:
        logger.warning("ReportLab PDF fallback failed: %s", e)

    # 3) Word COM (last): highest fidelity on Windows when Word is present.
    try:
        import docx2pdf  # type: ignore

        docx2pdf.convert(str(docx_path), str(pdf_path))
        if pdf_path.is_file():
            logger.info("DOCX->PDF via docx2pdf -> %s", pdf_path)
            return pdf_path.resolve()
    except Exception as e:
        logger.debug("docx2pdf failed: %s", e)

    raise RuntimeError(
        "PDF export failed: LibreOffice (soffice), ReportLab + system fonts, or Word + "
        "docx2pdf could not produce a PDF."
    )


def _find_soffice() -> Path | None:
    cfg = get_pipeline_settings().libreoffice_soffice_path
    if cfg:
        p = Path(cfg)
        if p.is_file():
            return p
    w = shutil.which("soffice") or shutil.which("soffice.exe")
    if w:
        return Path(w)
    for candidate in _libreoffice_soffice_candidates():
        if candidate.is_file():
            return candidate
    return None


def _libreoffice_soffice_candidates() -> list[Path]:
    """Common install locations when soffice is not on PATH (typical on Windows)."""
    import os
    import platform

    if platform.system() != "Windows":
        return []
    program_files = os.environ.get("ProgramFiles", r"C:\Program Files")
    program_files_x86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
    bases = [Path(program_files), Path(program_files_x86)]
    out: list[Path] = []
    for base in bases:
        out.append(base / "LibreOffice" / "program" / "soffice.exe")
        out.append(base / "OpenOffice 4" / "program" / "soffice.exe")
    return out
