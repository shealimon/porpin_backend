"""Smoke: document_pipeline package and orchestrator are importable."""

from app.services.document_pipeline import (
    resolve_document_template,
    run_translate_export_docx_pipeline,
)
from app.services.document_pipeline.stages import extract_text_blocks
from app.services.pipeline_runner import run_pipeline


def test_resolve_template_defaults():
    assert resolve_document_template("nope") == "report"
    assert resolve_document_template("ebook") == "ebook"


def test_all_exports_reachable():
    assert callable(run_translate_export_docx_pipeline)
    assert callable(extract_text_blocks)
    assert callable(run_pipeline)
