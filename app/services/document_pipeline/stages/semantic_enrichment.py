"""Metadata extraction + structure detection **before** translation.

Runs after :func:`~app.services.document_pipeline.stages.classify.classify_source_blocks`
so ``SectionAction`` / ``StructuralTag`` are stable. Mutates **copies** of blocks with
``ContentBlock.semantic_role`` and normalized ``level`` for outline-aware export.
"""

from __future__ import annotations

import re
from app.models.document_models import (
    BlockType,
    ClassifiedBlock,
    ContentBlock,
    SectionAction,
    StructuralTag,
)
from app.models.document_semantics import DocumentKind, DocumentMetadata
from app.services.formatter.book_structure import should_exclude_from_exported_toc
from app.services.formatter.chapter_heading_policy import chapter_like_heading_text
from app.services.parser.document_structure_refinement import (
    AMBIGUOUS_HEADING_LEMMAS,
    heading_subtitle_pair_supported,
)
from app.utils.translate_filter import count_words

_FALSE_HEADING_MIN_WORDS = 26

_MULTI_SENT = re.compile(r"""[.!?…][""'\u201d\u2019)\]]*\s+[A-Z\u00c0-\u1fff]""")

_JEE_NEET = re.compile(
    r"(?i)\b(jee|neet|iit|jee\s*main|jee\s*advanced|nta|mock\s*test|previous\s+year)\b",
)
_WORKSHEET = re.compile(r"(?i)\b(worksheet|homework|exercise\s*[:\-])\b")
_QA = re.compile(r"(?i)^\s*(q\d+[\.)]\s+|question\s*\d+|\d+[\.)]\s+.+[?？]\s*$)")


def _multi_sentence_prose(t: str) -> bool:
    if len(t) < 42:
        return False
    return bool(_MULTI_SENT.search(t))


def _infer_document_type(classified: list[ClassifiedBlock]) -> DocumentKind:
    parts: list[str] = []
    chapters = 0
    lists = 0
    paras = 0
    for cb in classified:
        if cb.action == SectionAction.OMIT:
            continue
        b = cb.block
        if b.type == BlockType.HEADING and b.text and chapter_like_heading_text(b.text):
            chapters += 1
        if b.type == BlockType.LIST:
            lists += 1
        if b.type == BlockType.PARAGRAPH:
            paras += 1
        if b.text:
            parts.append(b.text.lower())
        if len(parts) >= 60:
            break
    blob = "\n".join(parts)
    if _JEE_NEET.search(blob) or _QA.search(blob):
        return "educational"
    if _WORKSHEET.search(blob):
        return "educational"
    if re.search(r"(?im)^abstract\s*$", blob) or " abstract " in f" {blob} ":
        return "article"
    if re.search(r"(?im)^(references|bibliography)\s*$", blob[-8000:]):
        if paras > lists:
            return "article"
    if chapters >= 3:
        return "book"
    if lists >= max(4, paras // 4) and paras > 0:
        return "notes"
    if re.search(r"(?im)^appendix\s+[a-z0-9]", blob):
        return "manual"
    if re.search(r"(?i)\b(table of contents|contents)\b", blob) and chapters >= 2:
        return "book"
    return "general"


def _demote_false_heading(
    cb: ClassifiedBlock, next_cb: ClassifiedBlock | None
) -> ClassifiedBlock:
    if cb.action != SectionAction.TRANSLATE:
        return cb
    b = cb.block
    if b.type != BlockType.HEADING or not (b.text and b.text.strip()):
        return cb
    if b.structural_tag in (
        StructuralTag.TITLE,
        StructuralTag.AUTHOR,
        StructuralTag.TOC,
    ):
        return cb
    t = b.text.strip()
    wc = count_words(t)
    if (
        len(t.split()) == 1
        and t.split()[0].lower() in AMBIGUOUS_HEADING_LEMMAS
        and next_cb
        and next_cb.action == SectionAction.TRANSLATE
        and heading_subtitle_pair_supported(next_cb.block)
    ):
        return cb
    if wc > _FALSE_HEADING_MIN_WORDS:
        return ClassifiedBlock(
            block=b.model_copy(
                update={
                    "type": BlockType.PARAGRAPH,
                    "level": 1,
                    "semantic_role": "paragraph",
                }
            ),
            action=cb.action,
        )
    if _multi_sentence_prose(t) and wc > 12:
        return ClassifiedBlock(
            block=b.model_copy(
                update={
                    "type": BlockType.PARAGRAPH,
                    "level": 1,
                    "semantic_role": "paragraph",
                }
            ),
            action=cb.action,
        )
    low = t.lower()
    if (t.endswith(",") or t.endswith(";")) and wc > 9 and not chapter_like_heading_text(t):
        return ClassifiedBlock(
            block=b.model_copy(
                update={
                    "type": BlockType.PARAGRAPH,
                    "level": 1,
                    "semantic_role": "paragraph",
                }
            ),
            action=cb.action,
        )
    # Clause-like short headings that are usually prose openings (handled in PDF parser too).
    if "," in t and wc >= 6 and not chapter_like_heading_text(t):
        return ClassifiedBlock(
            block=b.model_copy(
                update={
                    "type": BlockType.PARAGRAPH,
                    "level": 1,
                    "semantic_role": "paragraph",
                }
            ),
            action=cb.action,
        )
    return cb


def _apply_quote_semantics(cb: ClassifiedBlock) -> ClassifiedBlock:
    if cb.action != SectionAction.TRANSLATE or cb.block.type != BlockType.PARAGRAPH:
        return cb
    if not cb.block.text:
        return cb
    if cb.block.semantic_role:
        return cb
    t = cb.block.text.lstrip()
    if not t:
        return cb
    if t[0] in "\u201c\u201e\xab\u00ab\"«":
        return ClassifiedBlock(
            block=cb.block.model_copy(update={"semantic_role": "quote"}),
            action=cb.action,
        )
    return cb


def _tag_front_matter_semantics(blocks: list[ClassifiedBlock]) -> None:
    for cb in blocks:
        b = cb.block
        if b.structural_tag == StructuralTag.TITLE and b.type != BlockType.TABLE:
            cb.block = b.model_copy(update={"semantic_role": "document_title"})
        elif b.structural_tag == StructuralTag.AUTHOR and b.type != BlockType.TABLE:
            cb.block = b.model_copy(update={"semantic_role": "author"})


def _find_subtitle_candidate(blocks: list[ClassifiedBlock]) -> int | None:
    """Return index of a block to treat as subtitle, or None."""

    first_toc = next(
        (
            i
            for i, cb in enumerate(blocks)
            if cb.block.structural_tag == StructuralTag.TOC
        ),
        len(blocks),
    )
    saw_title = False
    for i, cb in enumerate(blocks):
        if i >= first_toc:
            break
        if cb.action == SectionAction.OMIT:
            continue
        b = cb.block
        if b.structural_tag == StructuralTag.TITLE:
            saw_title = True
            continue
        if b.structural_tag == StructuralTag.AUTHOR:
            continue
        if not saw_title:
            continue
        if b.type == BlockType.HEADING and b.text:
            t = b.text.strip()
            wc = count_words(t)
            if (
                wc <= 14
                and len(t) <= 120
                and not chapter_like_heading_text(t)
                and not should_exclude_from_exported_toc(t)
            ):
                return i
        if b.type == BlockType.PARAGRAPH and b.text:
            t = b.text.strip()
            wc = count_words(t)
            if 2 <= wc <= 22 and len(t) <= 200 and not should_exclude_from_exported_toc(t):
                if not re.search(r"[.!?]\s+[A-Z]", t):
                    return i
    return None


def _apply_heading_outline(blocks: list[ClassifiedBlock]) -> list[str]:
    """Assign semantic_role chapter/heading/subheading and normalized outline ``level`` (1–3)."""
    chapter_titles: list[str] = []
    prev_leaf: str | None = None
    for cb in blocks:
        if cb.action == SectionAction.OMIT:
            continue
        b = cb.block
        if getattr(b, "semantic_role", None) in ("subtitle", "document_title", "author"):
            prev_leaf = "body"
            continue
        if b.structural_tag in (
            StructuralTag.TITLE,
            StructuralTag.AUTHOR,
            StructuralTag.TOC,
        ):
            continue
        if b.type != BlockType.HEADING or not (b.text and b.text.strip()):
            if b.type in (BlockType.PARAGRAPH, BlockType.LIST, BlockType.TABLE):
                prev_leaf = "body"
            continue
        text = b.text.strip()
        is_ch = chapter_like_heading_text(text)
        if is_ch:
            chapter_titles.append(text)
            cb.block = b.model_copy(
                update={
                    "level": 1,
                    "semantic_role": "chapter",
                }
            )
            prev_leaf = "chapter"
            continue
        if prev_leaf in (None, "chapter", "body"):
            cb.block = b.model_copy(
                update={
                    "level": 2,
                    "semantic_role": "heading",
                }
            )
            prev_leaf = "heading"
        else:
            cb.block = b.model_copy(
                update={
                    "level": 3,
                    "semantic_role": "subheading",
                }
            )
            prev_leaf = "subheading"
    return chapter_titles


def enrich_classified_blocks(
    classified: list[ClassifiedBlock],
) -> tuple[list[ClassifiedBlock], DocumentMetadata]:
    """Return new list with semantics filled; does not mutate the caller's ``classified`` list."""
    out = [
        ClassifiedBlock(block=cb.block.model_copy(), action=cb.action)
        for cb in classified
    ]
    doc_type = _infer_document_type(out)
    for i, cb in enumerate(out):
        nxt = out[i + 1] if i + 1 < len(out) else None
        out[i] = _demote_false_heading(cb, nxt)
    _tag_front_matter_semantics(out)
    sub_i = _find_subtitle_candidate(out)
    if sub_i is not None and out[sub_i].action == SectionAction.TRANSLATE:
        b = out[sub_i].block
        out[sub_i] = ClassifiedBlock(
            block=b.model_copy(update={"semantic_role": "subtitle"}),
            action=out[sub_i].action,
        )
    chapter_titles = _apply_heading_outline(out)
    for i, cb in enumerate(out):
        out[i] = _apply_quote_semantics(cb)
    meta = DocumentMetadata(
        document_type=doc_type,
        subtitle_block_index=sub_i,
        detected_chapter_titles=chapter_titles[:200],
    )
    return out, meta


# Re-export for callers that only need the tuple destructure
enrich_classified = enrich_classified_blocks
