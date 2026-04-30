"""JSON (de)serialization for :class:`translation_plan.BlockWork` + :class:`ParagraphPiece`."""

from __future__ import annotations

import json
from typing import Any

from app.models.document_models import ClassifiedBlock
from app.services.translation_plan import (
    BlockWork,
    ParagraphPiece,
    SkipBlockWork,
    TableBlockWork,
    TextBlockWork,
)


def _piece_to_json(p: ParagraphPiece) -> dict[str, Any]:
    return {"structure": [[k, v] for k, v in p.structure]}


def _piece_from_json(d: dict[str, Any]) -> ParagraphPiece:
    pairs: list[tuple[str, str | int]] = []
    for row in d["structure"]:
        k, v = row[0], row[1]
        if k == "job":
            v = int(v)
        pairs.append((k, v))
    return ParagraphPiece(tuple(pairs))


def block_work_to_jsonable(w: BlockWork) -> dict[str, Any]:
    if isinstance(w, SkipBlockWork):
        return {
            "t": "skip",
            "block_index": w.block_index,
            "classified": w.classified.model_dump(mode="json"),
        }
    if isinstance(w, TextBlockWork):
        return {
            "t": "text",
            "block_index": w.block_index,
            "classified": w.classified.model_dump(mode="json"),
            "paragraphs": [_piece_to_json(p) for p in w.paragraphs],
        }
    if isinstance(w, TableBlockWork):
        cells_j: list[list[Any]] = []
        for row in w.cells:
            rj: list[Any] = []
            for cell in row:
                if cell is None:
                    rj.append(None)
                else:
                    rj.append([_piece_to_json(p) for p in cell])
            cells_j.append(rj)
        return {
            "t": "table",
            "block_index": w.block_index,
            "classified": w.classified.model_dump(mode="json"),
            "cells": cells_j,
        }
    raise TypeError(type(w))


def block_work_from_jsonable(d: dict[str, Any]) -> BlockWork:
    t = d["t"]
    bi = int(d["block_index"])
    cb = ClassifiedBlock.model_validate(d["classified"])
    if t == "skip":
        return SkipBlockWork(block_index=bi, classified=cb)
    if t == "text":
        paras = [_piece_from_json(p) for p in d["paragraphs"]]
        return TextBlockWork(block_index=bi, classified=cb, paragraphs=paras)
    if t == "table":
        cells: list[list[list[ParagraphPiece] | None]] = []
        for row in d["cells"]:
            cr: list[list[ParagraphPiece] | None] = []
            for cell in row:
                if cell is None:
                    cr.append(None)
                else:
                    cr.append([_piece_from_json(x) for x in cell])
            cells.append(cr)
        return TableBlockWork(block_index=bi, classified=cb, cells=cells)
    raise ValueError(f"unknown block work type {t}")


def dump_manifest_v2(
    *,
    segments: list[str],
    batches: list[list[int]],
    block_work: list[BlockWork],
    document_template_id: str | None = None,
    translation_target: str | None = None,
) -> dict[str, Any]:
    d: dict[str, Any] = {
        "version": 2,
        "segments": segments,
        "batches": batches,
        "block_work": [block_work_to_jsonable(w) for w in block_work],
    }
    if document_template_id and str(document_template_id).strip():
        d["document_template_id"] = str(document_template_id).strip()
    if translation_target and str(translation_target).strip():
        d["translation_target"] = str(translation_target).strip().lower()
    return d


def load_manifest_v2(data: dict[str, Any]) -> tuple[list[str], list[list[int]], list[BlockWork]]:
    if int(data.get("version", 0)) != 2:
        raise ValueError("unsupported manifest version")
    segments = list(data["segments"])
    batches = [list(map(int, b)) for b in data["batches"]]
    bw = [block_work_from_jsonable(x) for x in data["block_work"]]
    return segments, batches, bw


def manifest_json_bytes(manifest: dict[str, Any]) -> bytes:
    return json.dumps(manifest, ensure_ascii=False).encode("utf-8")
