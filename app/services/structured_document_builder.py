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
from app.models.document_semantics import DocumentMetadata
from app.models.structured_document import (
    ContentTag,
    HeadingSemanticKind,
    StructuredDocument,
    StructuredHeading,
    StructuredList,
    StructuredParagraph,
    StructuredTable,
)
from app.services.formatter.book_typography import strip_markdown_artifacts


def _reflow_wrapped_paragraph_text(s: str) -> str:
    """Collapse PDF/Word hard line wraps (single newlines) into spaces.

    Blank-line gaps (``\\n\\n+``) are kept so one block can still represent multiple
    logical stanzas; each stanza is reflowed to a single line of text.
    """
    t = (s or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not t:
        return ""
    chunks = [c for c in re.split(r"\n\s*\n+", t) if c.strip()]
    reflowed: list[str] = []
    for c in chunks:
        line = " ".join(ln.strip() for ln in c.split("\n") if ln.strip())
        if line:
            reflowed.append(line)
    return "\n\n".join(reflowed)


def _clean_text(s: str | None) -> str:
    raw = strip_markdown_artifacts(s or "").strip()
    return _reflow_wrapped_paragraph_text(raw)


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


def _pop_subtitle_from_tail(tail: list[ClassifiedBlock]) -> tuple[str | None, list[ClassifiedBlock]]:
    """Lift ``semantic_role=subtitle`` blocks into a single string; drop them from body flow."""
    lines: list[str] = []
    kept: list[ClassifiedBlock] = []
    for item in tail:
        if item.action == SectionAction.OMIT:
            continue
        b = item.block
        if getattr(b, "semantic_role", None) == "subtitle":
            t = _clean_text(b.text or "")
            if t:
                lines.append(t)
            continue
        kept.append(item)
    return ("\n".join(lines) if lines else None), kept


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
    items = [
        _normalize_list_item_line(_reflow_wrapped_paragraph_text(strip_markdown_artifacts(ln).strip()), ordered=ordered)
        for ln in raw_lines
    ]
    items = [x for x in items if x]
    if not items:
        raw = _clean_text(block.text)
        if raw:
            items = [_normalize_list_item_line(raw, ordered=ordered)]
        items = [x for x in items if x]
    return items, ordered


def _heading_kind_from_role(role: str | None) -> HeadingSemanticKind | None:
    if role in ("chapter", "heading", "subheading"):
        return role  # type: ignore[return-value]
    return None


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
            kind=_heading_kind_from_role(getattr(b, "semantic_role", None)),
        )
    if b.type == BlockType.PARAGRAPH and b.text:
        return StructuredParagraph(
            text=_clean_text(b.text),
            content_tag=content_tag,
            is_quote=getattr(b, "semantic_role", None) == "quote",
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


def build_structured_document(
    classified: list[ClassifiedBlock],
    *,
    document_metadata: DocumentMetadata | None = None,
) -> StructuredDocument:
    # Safety net: template/PDF paths must not show PDF echo duplicates even if a caller skipped translate-time dedupe.
    from app.services.document_pipeline.paragraph_overlap_dedupe import (
        dedupe_consecutive_redundant_translate_paragraphs,
    )

    classified = dedupe_consecutive_redundant_translate_paragraphs(list(classified))
    title_items, author_items, tail = _partition_front_matter(classified)
    title = _merge_title_lines(title_items)
    authors = _author_lines(author_items)
    subtitle, tail = _pop_subtitle_from_tail(tail)

    content: list = []
    for item in tail:
        if item.action == SectionAction.OMIT:
            continue
        # TOC is classified SKIP: never copy printed/nav TOC into structured export/HTML —
        # headings-based TOC is added separately when rendering.
        if item.action == SectionAction.SKIP:
            continue
        tag: ContentTag = (
            "toc" if item.block.structural_tag == StructuralTag.TOC else "body"
        )
        node = _block_to_structured(item, content_tag=tag)
        if node is not None:
            content.append(node)

    return StructuredDocument(
        title=title,
        subtitle=subtitle,
        authors=authors,
        document_type=(document_metadata.document_type if document_metadata else None),
        content=content,
    )


def structured_json_path_for_docx(docx_path: Path) -> Path:
    """Sidecar path used by the pipeline (same directory, ``{stem}.structure.json``)."""
    return docx_path.parent / f"{docx_path.stem}.structure.json"


def write_structured_document_json(
    classified: list[ClassifiedBlock],
    out_path: Path,
    *,
    document_metadata: DocumentMetadata | None = None,
) -> Path:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    doc = build_structured_document(classified, document_metadata=document_metadata)
    out_path.write_text(
        doc.model_dump_json(indent=2),
        encoding="utf-8",
    )
    return out_path.resolve()
