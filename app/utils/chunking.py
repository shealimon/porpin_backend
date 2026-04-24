"""Token-bounded chunking with paragraph/sentence-friendly splits."""

from __future__ import annotations

import re

import tiktoken

from app.core.pipeline_settings import get_pipeline_settings


def _encoding():
    try:
        return tiktoken.encoding_for_model("gpt-4o")
    except KeyError:
        return tiktoken.get_encoding("cl100k_base")


def count_tokens(text: str) -> int:
    enc = _encoding()
    return len(enc.encode(text or ""))


def chunk_text_respecting_paragraphs(text: str) -> list[str]:
    """
    Split into chunks between chunk_min_tokens and chunk_max_tokens when possible.
    Prefer paragraph boundaries, then newlines, then sentence endings.
    """
    s = get_pipeline_settings()
    min_tok = s.chunk_min_tokens
    max_tok = s.chunk_max_tokens
    text = (text or "").strip()
    if not text:
        return []

    paragraphs = [p.strip() for p in re.split(r"\n\s*\n+", text) if p.strip()]
    if not paragraphs:
        paragraphs = [text]

    chunks: list[str] = []
    current: list[str] = []
    current_tokens = 0

    def flush():
        nonlocal current, current_tokens
        if current:
            chunks.append("\n\n".join(current))
        current = []
        current_tokens = 0

    for para in paragraphs:
        ptoks = count_tokens(para)
        if ptoks > max_tok:
            flush()
            chunks.extend(_split_oversized_paragraph(para, min_tok, max_tok))
            continue
        if current_tokens + ptoks > max_tok and current_tokens >= min_tok:
            flush()
        elif current_tokens + ptoks > max_tok and current:
            flush()
        current.append(para)
        current_tokens += ptoks
        if current_tokens >= max_tok:
            flush()

    flush()
    return [c for c in chunks if c.strip()]


def _split_oversized_paragraph(para: str, min_tok: int, max_tok: int) -> list[str]:
    sentences = re.split(r"(?<=[.!?])\s+", para)
    sentences = [s.strip() for s in sentences if s.strip()]
    if len(sentences) <= 1:
        return _hard_split_words(para, max_tok)

    out: list[str] = []
    cur: list[str] = []
    ct = 0
    for sent in sentences:
        st = count_tokens(sent)
        if st > max_tok:
            if cur:
                out.append(" ".join(cur))
                cur = []
                ct = 0
            out.extend(_hard_split_words(sent, max_tok))
            continue
        if ct + st > max_tok and ct >= min_tok:
            out.append(" ".join(cur))
            cur = []
            ct = 0
        elif ct + st > max_tok and cur:
            out.append(" ".join(cur))
            cur = []
            ct = 0
        cur.append(sent)
        ct += st
    if cur:
        out.append(" ".join(cur))
    return out


def _hard_split_words(text: str, max_tok: int) -> list[str]:
    words = text.split()
    out: list[str] = []
    cur: list[str] = []
    ct = 0
    for w in words:
        wt = count_tokens(w + " ")
        if ct + wt > max_tok and cur:
            out.append(" ".join(cur))
            cur = []
            ct = 0
        cur.append(w)
        ct += wt
    if cur:
        out.append(" ".join(cur))
    return out
