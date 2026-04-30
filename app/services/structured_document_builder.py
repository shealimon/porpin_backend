"""Build ``StructuredDocument`` from translated ``ClassifiedBlock`` list (plain text only)."""

from __future__ import annotations

import re
from pathlib import Path

from app.models.document_models import (
    BlockType,
    ClassifiedBlock,
    ContentBlock,
    SectionAction,
    StructuralTag,
)
from app.models.structured_document import (
    ContentTag,
    StructuredDocument,
    StructuredHeading,
    StructuredList,
    StructuredParagraph,
    StructuredTable,
)
from app.services.formatter.book_typography import strip_markdown_artifacts


def _clean_text(s: str | None) -> str:
    return strip_markdown_artifacts(s or "").strip()


def _merge_title_lines(items: list[ClassifiedBlock]) -> str | None:
    parts: list[str] = []
    for it in items:
        t = _clean_text(it.block.text)
        if t:
            parts.append(t)
    if not parts:
        return None
    return "\n".join(parts)


def _author_lines(items: list[ClassifiedBlock]) -> list[str]:
    lines: list[str] = []
    for it in items:
        t = _clean_text(it.block.text)
        if not t:
            continue
        for part in t.split("\n"):
            s = part.strip()
            if s:
                lines.append(s)
    return lines


def _partition_front_matter(
    blocks: list[ClassifiedBlock],
) -> tuple[list[ClassifiedBlock], list[ClassifiedBlock], list[ClassifiedBlock]]:
    title: list[ClassifiedBlock] = []
    author: list[ClassifiedBlock] = []
    rest: list[ClassifiedBlock] = []
    for item in blocks:
        if item.action == SectionAction.OMIT:
            continue
        b = item.block
        if b.structural_tag == StructuralTag.TITLE and b.type != BlockType.TABLE:
            title.append(item)
        elif b.structural_tag == StructuralTag.AUTHOR and b.type != BlockType.TABLE:
            author.append(item)
        else:
            rest.append(item)
    return title, author, rest


def _infer_ordered_from_items(items: list[str]) -> bool:
    for it in items:
        if re.match(r"^\d+[\.)]\s+", it.lstrip()):
            return True
    return False


def _normalize_list_item_line(line: str, *, ordered: bool) -> str:
    t = _clean_text(line)
    if ordered:
        t = re.sub(r"^\d+[\.)]\s+", "", t)
    else:
        t = re.sub(r"^[-*•]\s+", "", t)
    return t.strip()


def _list_items_and_ordered(block: ContentBlock) -> tuple[list[str], bool]:
    raw_lines = [ln for ln in (block.text or "").splitlines() if ln.strip()]
    if block.list_kind == "ordered":
        ordered = True
    elif block.list_kind == "bullet":
        ordered = False
    else:
        ordered = _infer_ordered_from_items([ln.strip() for ln in raw_lines])
    items = [_normalize_list_item_line(ln, ordered=ordered) for ln in raw_lines]
    items = [x for x in items if x]
    if not items:
        raw = _clean_text(block.text)
        if raw:
            items = [_normalize_list_item_line(raw, ordered=ordered)]
        items = [x for x in items if x]
    return items, ordered


def _block_to_structured(
    item: ClassifiedBlock,
    *,
    content_tag: ContentTag,
) -> StructuredHeading | StructuredParagraph | StructuredList | StructuredTable | None:
    b = item.block
    if b.type == BlockType.HEADING and b.text:
        return StructuredHeading(
            level=max(1, min(9, b.level)),
            text=_clean_text(b.text),
            content_tag=content_tag,
        )
    if b.type == BlockType.PARAGRAPH and b.text:
        return StructuredParagraph(
            text=_clean_text(b.text),
            content_tag=content_tag,
        )
    if b.type == BlockType.LIST and b.text:
        items, ordered = _list_items_and_ordered(b)
        if not items:
            return None
        return StructuredList(
            ordered=ordered,
            items=items,
            content_tag=content_tag,
        )
    if b.type == BlockType.TABLE and b.data:
        rows: list[list[str]] = []
        for row in b.data:
            rows.append([_clean_text(c) for c in row])
        if not any(any(c for c in r) for r in rows):
            return None
        return StructuredTable(rows=rows, content_tag=content_tag)
    return None


def build_structured_document(classified: list[ClassifiedBlock]) -> StructuredDocument:
    title_items, author_items, tail = _partition_front_matter(classified)
    title = _merge_title_lines(title_items)
    authors = _author_lines(author_items)

    content: list = []
    for item in tail:
        if item.action == SectionAction.OMIT:
            continue
        tag: ContentTag = (
            "toc" if item.block.structural_tag == StructuralTag.TOC else "body"
        )
        node = _block_to_structured(item, content_tag=tag)
        if node is not None:
            content.append(node)

    return StructuredDocument(title=title, authors=authors, content=content)


def structured_json_path_for_docx(docx_path: Path) -> Path:
    """Sidecar path used by the pipeline (same directory, ``{stem}.structure.json``)."""
    return docx_path.parent / f"{docx_path.stem}.structure.json"


def write_structured_document_json(
    classified: list[ClassifiedBlock],
    out_path: Path,
) -> Path:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    doc = build_structured_document(classified)
    out_path.write_text(
        doc.model_dump_json(indent=2),
        encoding="utf-8",
    )
    return out_path.resolve()
