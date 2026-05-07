"""PDF flush: bold at body point-size should still become HEADING blocks."""

from __future__ import annotations

from app.models.document_models import BlockType
from app.services.parser.pdf_parser import _flush_pdf_line_buffer, _line_bold_strength


def test_line_bold_strength_all_bold_spans():
    line = {
        "spans": [
            {"text": "Introduction", "font": "Serif-Bold", "flags": 0},
        ]
    }
    assert _line_bold_strength(line) >= 0.99


def test_flush_bold_same_point_size_becomes_heading():
    body_font = 11.0
    # Same size as body, strong bold signal — typical trade-PDF section title.
    buf = [(11.0, "The Power of Habits", 0.95)]
    out = _flush_pdf_line_buffer(buf, body_font, 1)
    assert len(out) == 1
    assert out[0].type == BlockType.HEADING
    assert out[0].level in (2, 3)


def test_flush_long_bold_sentence_stays_paragraph():
    body_font = 11.0
    text = " ".join(["word"] * 16) + "."
    buf = [(11.0, text, 0.95)]
    out = _flush_pdf_line_buffer(buf, body_font, 1)
    assert len(out) == 1
    assert out[0].type == BlockType.PARAGRAPH
