"""Detect printed *Part* splash pages (mini-TOC: Part N + 3+ N.M rows) and omit from export."""

from __future__ import annotations

import re

from app.models.document_models import BlockType, ContentBlock, StructuralTag
from app.utils.translate_filter import count_words

_LONE_ROMAN_PART_DIGIT = re.compile(r"^\d{1,3}$")
_PART_OPENER = re.compile(r"(?i)^part\s+(\d{1,3})\b")
_SUBSECTION_DECIMAL = re.compile(r"\b(\d+)\.(\d+)\b")
_ORPHAN_ENUM = re.compile(r"^\d{1,2}\s*\.\s*$")


def _subsection_hits(text: str) -> list[tuple[int, int]]:
    return [(int(m.group(1)), int(m.group(2))) for m in _SUBSECTION_DECIMAL.finditer(text or "")]


def _try_part_subsection_inventory_run(
    blocks: list[ContentBlock],
    start: int,
) -> tuple[int, int] | None:
    """If ``blocks[start:end]`` is a Part splash + subsection list, return ``(start, end)`` (end exclusive)."""
    n = len(blocks)
    if start >= n:
        return None
    b0 = blocks[start]
    if b0.structural_tag in (
        StructuralTag.TITLE,
        StructuralTag.AUTHOR,
        StructuralTag.TOC,
    ):
        return None
    i = start
    t0 = (b0.text or "").strip()
    if b0.type == BlockType.HEADING and _LONE_ROMAN_PART_DIGIT.match(t0):
        i += 1
        if i >= n:
            return None
        b0 = blocks[i]
        t0 = (b0.text or "").strip()
    m = _PART_OPENER.match(t0)
    if not m:
        return None
    major = int(m.group(1))
    if count_words(t0) > 24:
        return None
    i += 1
    hits: list[tuple[int, int]] = []
    while i < n:
        bb = blocks[i]
        if bb.structural_tag in (
            StructuralTag.TITLE,
            StructuralTag.AUTHOR,
            StructuralTag.TOC,
        ):
            break
        txt = (bb.text or "").strip()
        if not txt:
            i += 1
            continue
        low = txt.lower()
        if low in ("preface", "introduction", "foreword", "prologue"):
            break
        if re.match(r"(?i)^(preface|introduction|foreword)\b", txt) and count_words(txt) >= 8:
            break
        if bb.type == BlockType.PARAGRAPH and count_words(txt) > 55:
            sub_probe = _subsection_hits(txt)
            if len(sub_probe) < 2:
                break
        sub = _subsection_hits(txt)
        if sub:
            if not all(mj == major for mj, _mn in sub):
                break
            hits.extend(sub)
            i += 1
            continue
        if _ORPHAN_ENUM.match(txt) or re.match(r"^\d{1,2}\.\s*$", txt):
            i += 1
            continue
        if bb.type == BlockType.HEADING:
            if re.match(r"(?i)^conclusion\b", txt) and len(txt) < 220:
                i += 1
                continue
            if low in ("acknowledgments", "acknowledgements"):
                i += 1
                continue
        break
    if len(hits) < 3:
        return None
    while i < n:
        bb = blocks[i]
        txt = (bb.text or "").strip()
        if not txt:
            i += 1
            continue
        low = txt.lower()
        if low in ("preface", "introduction", "foreword", "prologue"):
            break
        if re.match(r"(?i)^(preface|introduction|foreword)\b", txt) and count_words(txt) >= 8:
            break
        if bb.type == BlockType.HEADING:
            if re.match(r"(?i)^conclusion\b", txt) and len(txt) < 220:
                i += 1
                continue
            if low in ("acknowledgments", "acknowledgements"):
                i += 1
                continue
        break
    return (start, i)


def part_subsection_inventory_indices(blocks: list[ContentBlock]) -> set[int]:
    """Block indices to ``OMIT`` — Part N printed inventories, not real reading body."""
    omit: set[int] = set()
    i = 0
    n = len(blocks)
    while i < n:
        run = _try_part_subsection_inventory_run(blocks, i)
        if run:
            s, e = run
            omit.update(range(s, e))
            i = e
        else:
            i += 1
    return omit
