"""Plain-text parser: paragraphs and simple single-level lists."""

from __future__ import annotations

import re
from pathlib import Path

from app.models.document_models import BlockType, ContentBlock


def _word_count_str(s: str) -> int:
    return len(re.findall(r"\S+", (s or "").strip()))


def _trim_words(s: str, max_words: int) -> str:
    words = re.findall(r"\S+", (s or "").strip())
    if len(words) <= max_words:
        return (s or "").strip()
    return " ".join(words[:max_words])


def parse_txt(
    path: Path,
    max_preview_words: int | None = None,
) -> list[ContentBlock]:
    raw = path.read_text(encoding="utf-8", errors="replace")
    blocks: list[ContentBlock] = []
    used = 0
    budget = max_preview_words
    paragraphs = re.split(r"\n\s*\n+", raw.strip())
    for chunk in paragraphs:
        if budget is not None and used >= budget:
            break
        lines = [ln.rstrip() for ln in chunk.splitlines() if ln.strip()]
        if not lines:
            continue
        if all(_is_bullet_line(ln) for ln in lines):
            text = "\n".join(_strip_bullet(ln) for ln in lines)
            ordered = all(
                bool(re.match(r"^\d+[\.)]\s+", ln.lstrip())) for ln in lines
            )
            list_kind = "ordered" if ordered else "bullet"
            w = _word_count_str(text)
            if budget is None:
                blocks.append(
                    ContentBlock(type=BlockType.LIST, text=text, list_kind=list_kind)
                )
                continue
            remain = budget - used
            if remain < 1:
                break
            if w <= remain:
                blocks.append(
                    ContentBlock(type=BlockType.LIST, text=text, list_kind=list_kind)
                )
                used += w
            else:
                blocks.append(
                    ContentBlock(
                        type=BlockType.LIST,
                        text=_trim_words(text, remain),
                        list_kind=list_kind,
                    )
                )
                break
            continue
        body = chunk.strip()
        w = _word_count_str(body)
        if budget is None:
            blocks.append(ContentBlock(type=BlockType.PARAGRAPH, text=body))
            continue
        remain = budget - used
        if remain < 1:
            break
        if w <= remain:
            blocks.append(ContentBlock(type=BlockType.PARAGRAPH, text=body))
            used += w
        else:
            blocks.append(
                ContentBlock(type=BlockType.PARAGRAPH, text=_trim_words(body, remain))
            )
            break
    return blocks


def _is_bullet_line(line: str) -> bool:
    s = line.lstrip()
    return bool(
        re.match(r"^[-*•]\s+", s)
        or re.match(r"^\d+[\.)]\s+", s)
    )


def _strip_bullet(line: str) -> str:
    s = line.strip()
    return re.sub(r"^[-*•]\s+", "", re.sub(r"^\d+[\.)]\s+", "", s))
