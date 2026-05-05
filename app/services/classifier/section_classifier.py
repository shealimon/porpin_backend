"""Rule-based section labeling: TRANSLATE vs SKIP vs OMIT.

- **TOC / Contents** (``StructuralTag.TOC``): ``SKIP`` — show in output, no translation.
- **index, glossary, references, appendix, copyright** (and related *section titles*): ``OMIT`` —
  no translation API and omitted from rebuilt DOCX / PDF. Section titles are matched with
  **tight patterns** (not loose substrings) so normal prose is not affected. Regions run until
  the next heading at the same or higher level — **no** early exit on long paragraphs (indexes
  and glossaries are often many pages).
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
from app.services.formatter.book_structure import should_exclude_from_exported_toc
from app.services.parser.pdf_running_header import looks_like_pdf_running_header_line
from app.utils.translate_filter import count_words

# Distributor / mirror-site lines (e.g. ManyBooks) — omit from output; not author “visit my site”.
_AUTHOR_SITE_HINT = re.compile(
    r"(?i)\b(my|our)\s+(website|blog|site|page|books|novels)\b|"
    r"\bvisit\s+me\s+at\b|\bauthor'?s\s+website\b"
)


def _is_distributor_download_boilerplate(text: str | None) -> bool:
    if not text:
        return False
    t = text.strip()
    if not t or len(t) > 260:
        return False
    if _AUTHOR_SITE_HINT.search(t):
        return False
    low = t.lower()
    if "manybooks.net" in low or "manybooks.org" in low:
        return True
    if re.search(r"(?i)^\s*a\s+free\s+ebook\s+from\b", t):
        return True
    return False


def _normalized(text: str | None) -> str:
    if not text:
        return ""
    return " ".join(text.lower().split())


# Full heading/standalone line match only (normalized lowercased text). Not substring search —
# avoids "index" / "references" inside body sentences and saves API on full back-matter runs.
_BACK_MATTER_SECTION_HEADING = re.compile(
    r"""
    ^(
        index(es)? |
        (subject|author)\s+index |
        references? |
        reference\s+list |
        works?\s+cited |
        bibliography |
        citations? |
        glossary(\s+of\s+[\w\s\-:,'’/&;]+)? |
        appendix(\s+[\w\s\-:,'’/]+)? |
        annex(\s+[\w\s\-:,'’/]+)? |
        copyright( notice| information| page| ©)? |
        legal notices? |
        imprint |
        list\s+of\s+figures |
        list\s+of\s+tables |
        acknowledg(e)?ments? |
        about\s+the\s+authors?
    )$
    """,
    re.VERBOSE,
)


def _is_back_matter_section_heading(text: str | None) -> bool:
    """True if the line is *only* a known back-matter section title (multi-page safe — full match)."""
    n = _normalized(text)
    if not n or len(n) > 200:
        return False
    return bool(_BACK_MATTER_SECTION_HEADING.fullmatch(n))


def _line_triggers_standalone_omit(text: str | None) -> bool:
    """Short non-heading lines that are alone on a line (e.g. bold 'References')."""
    return _is_back_matter_section_heading(text)


def _looks_like_copyright_block(text: str | None) -> bool:
    """Short copyright / imprint lines (omit from translated export)."""
    if not text:
        return False
    t = text.strip()
    if len(t) > 600:
        return False
    low = t.lower()
    if "©" in t or "all rights reserved" in low:
        return True
    if re.search(r"\bisbn[\s:-]*[\d\-]{8,}", low):
        return True
    return False


def classify_blocks(blocks: list[ContentBlock]) -> list[ClassifiedBlock]:
    """
    TOC is ``SKIP``: shown in output unchanged (no translation).

    A back-matter **section** starts when a **heading** matches a tight section-title pattern
    (index, glossary, references, appendix, copyright, …). All blocks under that heading are
    ``OMIT`` until a heading at the same or higher outline level. Long paragraphs do **not**
    end the region (indexes/glossaries can span many pages).
    """
    out: list[ClassifiedBlock] = []
    skip_until_level: int | None = None

    for block in blocks:
        # Table of Contents / Contents: keep in output, original text (no translation).
        if block.structural_tag == StructuralTag.TOC:
            if should_exclude_from_exported_toc(block.text):
                out.append(
                    ClassifiedBlock(
                        block=block.model_copy(update={"structural_tag": None}),
                        action=SectionAction.OMIT,
                    )
                )
                continue
            out.append(ClassifiedBlock(block=block, action=SectionAction.SKIP))
            continue

        # Interior PDF runners / TOC bleed (often heading-shaped); never translate or export.
        if block.text and block.type in (
            BlockType.PARAGRAPH,
            BlockType.LIST,
            BlockType.HEADING,
        ):
            if looks_like_pdf_running_header_line(block.text):
                out.append(ClassifiedBlock(block=block, action=SectionAction.OMIT))
                continue

        # Title-page imprint spilled outside TOC rows (publisher / translator boilerplate).
        if (
            block.structural_tag
            not in (StructuralTag.TITLE, StructuralTag.AUTHOR, StructuralTag.TOC)
            and block.text
            and block.type in (BlockType.PARAGRAPH, BlockType.LIST)
            and count_words(block.text.strip()) <= 14
            and should_exclude_from_exported_toc(block.text)
        ):
            out.append(ClassifiedBlock(block=block, action=SectionAction.OMIT))
            continue

        if block.type == BlockType.HEADING and block.text:
            if _is_back_matter_section_heading(block.text):
                skip_until_level = block.level
                out.append(ClassifiedBlock(block=block, action=SectionAction.OMIT))
                continue
            if skip_until_level is not None and block.level <= skip_until_level:
                skip_until_level = None

        if skip_until_level is not None:
            out.append(ClassifiedBlock(block=block, action=SectionAction.OMIT))
            continue

        if block.type in (BlockType.PARAGRAPH, BlockType.LIST) and block.text:
            t = block.text.strip()
            if len(t) < 120 and _line_triggers_standalone_omit(t):
                if not re.search(r"[.!?]\s+[A-Z]", t):
                    out.append(ClassifiedBlock(block=block, action=SectionAction.OMIT))
                    continue
            if _looks_like_copyright_block(t):
                out.append(ClassifiedBlock(block=block, action=SectionAction.OMIT))
                continue

        if block.type in (BlockType.PARAGRAPH, BlockType.LIST, BlockType.HEADING):
            if _is_distributor_download_boilerplate(block.text):
                out.append(ClassifiedBlock(block=block, action=SectionAction.OMIT))
                continue

        out.append(ClassifiedBlock(block=block, action=SectionAction.TRANSLATE))

    return out
