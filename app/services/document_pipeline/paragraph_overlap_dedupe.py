"""Drop near-duplicate translated body text (split across blocks or glued in one paragraph)."""

from __future__ import annotations

import difflib
import re

from app.models.document_models import BlockType, ClassifiedBlock, SectionAction

# Hinglish metaphor hook: model sometimes writes "Yeh esi/esa ..." instead of "Yeh aisa...".
_HINGLISH_METAPHOR_VERB = r"(?:ais(?:e|a)|esa|esi)"
_HINGLISH_METAPHOR_FRAGMENT = rf"(?:ye|yeh)\s+{_HINGLISH_METAPHOR_VERB}\s+hai\s+jaise"

_INNER_VOICE_TRUNC_BEFORE_REPEAT = re.compile(
    rf"(?is)\b{_HINGLISH_METAPHOR_FRAGMENT}\b"
    r"[\s\S]{0,1500}?"
    rf'(?:[\u201c\u201d"]\s*)?STOP!\s*YEH?\s*EK\b\s*(?=\b{_HINGLISH_METAPHOR_FRAGMENT}\b)',
)


def _strip_inner_voice_truncation_echo(text: str) -> str:
    """Remove first truncated inner-voice clause when immediately re-written (same metaphor)."""
    cur = text
    for _ in range(5):
        nxt, n_sub = _INNER_VOICE_TRUNC_BEFORE_REPEAT.subn("", cur, count=1)
        if n_sub == 0:
            break
        cur = " ".join(nxt.split())
    return cur

_HINGLISH_METAPHOR_OPEN = re.compile(
    rf"(?is)\b{_HINGLISH_METAPHOR_FRAGMENT}\b",
)


_QUOTE_CHARS = frozenset(('"', "\u201c", "\u201d", "\u2018", "\u2019"))


def _normalize_for_overlap(text: str) -> str:
    t = " ".join((text or "").lower().split())
    out: list[str] = []
    for ch in t:
        if ch in _QUOTE_CHARS or ch == "`":
            continue
        out.append(ch)
    return "".join(out)


def _word_jaccard_prefix(a: str, b: str, *, max_words: int) -> float:
    wa = (a or "").lower().split()
    wb = (b or "").lower().split()
    k = min(max_words, len(wa), len(wb))
    if k < 6:
        return 0.0
    sa, sb = set(wa[:k]), set(wb[:k])
    u = len(sa | sb)
    if u == 0:
        return 0.0
    return len(sa & sb) / u


def _ascii_double_quote_unbalanced(s: str) -> bool:
    return s.count('"') % 2 == 1


def _ends_with_stop_ye_ek_fragment(s: str) -> bool:
    """Truncated inner-voice line from this book often ends here (with or without a closing quote)."""
    t = (s or "").rstrip()
    return bool(re.search(r'STOP!\s*YEH?\s*EK["\u201d\u2019\s]*$', t, re.I))


def _ends_with_sentence_punct(s: str) -> bool:
    t = s.rstrip()
    return bool(t) and t[-1] in ".!?"


def _likely_truncated_for_fuzzy_duplicate(shorter_text: str) -> bool:
    """True when the shorter paragraph plausibly got cut mid-thought / mid-quote."""
    t = shorter_text.strip()
    if len(t) < 20:
        return False
    if _ends_with_stop_ye_ek_fragment(t):
        return True
    if _ascii_double_quote_unbalanced(t):
        return True
    # Dialogue opened with curly quotes only (avoid counting apostrophes inside words).
    if t.count("\u201c") > t.count("\u201d"):
        return True
    return False


def _redundant_translate_paragraph_pair(prev: str, nxt: str) -> int | None:
    """Return 0 to drop prev, 1 to drop nxt; None keeps both."""
    a = (prev or "").strip()
    b = (nxt or "").strip()
    if len(a) < 24 or len(b) < 24:
        return None

    na, nb = _normalize_for_overlap(a), _normalize_for_overlap(b)

    if len(a) <= len(b):
        shorter_raw, longer_raw = a, b
        shorter_norm, longer_norm = na, nb
        shorter_is_prev = True
    else:
        shorter_raw, longer_raw = b, a
        shorter_norm, longer_norm = nb, na
        shorter_is_prev = False

    ext = len(longer_norm) - len(shorter_norm)
    if ext < 18:
        return None

    # 1) Normalized prefix continuation (avoid dropping a standalone sentence whose next
    #    paragraph happens to elaborate with the same opening clause).
    if (
        longer_norm.startswith(shorter_norm)
        and len(shorter_norm) >= 24
        and not _ends_with_sentence_punct(shorter_raw)
    ):
        return 0 if shorter_is_prev else 1

    # 2) Truncated / split quote duplicate: similarity + truncation cue on shorter text.
    if not _likely_truncated_for_fuzzy_duplicate(shorter_raw):
        return None

    j = _word_jaccard_prefix(a, b, max_words=22)
    ratio = difflib.SequenceMatcher(None, na, nb).ratio()
    # Shorter heuristic threshold when clipped at STOP! YE EK (norm can be shorter).
    min_ratio, min_j = (0.52, 0.42) if _ends_with_stop_ye_ek_fragment(shorter_raw) else (0.58, 0.48)
    if j < min_j or ratio < min_ratio:
        return None
    min_norm = (
        28 if _ends_with_stop_ye_ek_fragment(shorter_raw) else 36
    )
    if len(shorter_norm) < min_norm:
        return None

    return 0 if shorter_is_prev else 1


def _is_pipeline_paragraph_candidate(cb: ClassifiedBlock) -> bool:
    b = cb.block
    return (
        cb.action == SectionAction.TRANSLATE
        and b.type == BlockType.PARAGRAPH
        and not b.structural_tag
        and bool((b.text or "").strip())
    )


def _dedupe_consecutive_redundant_translate_paragraphs_once(
    classified: list[ClassifiedBlock],
) -> list[ClassifiedBlock]:
    """Single left-to-right pass."""
    if len(classified) < 2:
        return classified
    out: list[ClassifiedBlock] = []
    i = 0
    n = len(classified)
    while i < n:
        cur = classified[i]
        if (
            out
            and _is_pipeline_paragraph_candidate(out[-1])
            and _is_pipeline_paragraph_candidate(cur)
            and (out[-1].block.text and cur.block.text)
        ):
            drop = _redundant_translate_paragraph_pair(
                out[-1].block.text or "",
                cur.block.text or "",
            )
            if drop == 0:
                out.pop()
                continue
            if drop == 1:
                i += 1
                continue
        out.append(cur)
        i += 1
    return out


def _collapse_interior_near_duplicate_once(text: str) -> str | None:
    """If one paragraph holds a truncated duplicate then the full rewrite, drop the first span."""
    t = (text or "").strip()
    if len(t) < 48:
        return None
    spans = list(_HINGLISH_METAPHOR_OPEN.finditer(t))
    if len(spans) < 2:
        return None
    start1, start2 = spans[0].start(), spans[1].start()
    s1 = t[start1:start2].strip()
    tail_words = t[start2:].strip().split()
    if len(s1) < 24:
        return None
    chunk_end: int | None = None
    for end in range(len(tail_words), 5, -1):
        chunk = " ".join(tail_words[:end]).strip()
        if len(chunk) < 24:
            break
        if _redundant_translate_paragraph_pair(s1, chunk) == 0:
            chunk_end = end
            break
    if chunk_end is None:
        return None
    chunk_keep = " ".join(tail_words[:chunk_end]).strip()
    rest = " ".join(tail_words[chunk_end:]).strip()
    head = t[:start1].rstrip()
    return " ".join(p for p in (head, chunk_keep, rest) if p).strip()


def _collapse_interior_near_duplicate(text: str, *, max_rounds: int = 4) -> str:
    cur = text or ""
    for _ in range(max_rounds):
        nxt = _collapse_interior_near_duplicate_once(cur)
        if nxt is None:
            return cur
        cur = nxt
    return cur


def _sanitize_translate_paragraph_interior(cb: ClassifiedBlock) -> ClassifiedBlock:
    if not _is_pipeline_paragraph_candidate(cb):
        return cb
    old = cb.block.text or ""
    new_t = _strip_inner_voice_truncation_echo(old)
    new_t = _collapse_interior_near_duplicate(new_t)
    if new_t == old:
        return cb
    return ClassifiedBlock(block=cb.block.model_copy(update={"text": new_t}), action=cb.action)


def dedupe_consecutive_redundant_translate_paragraphs(
    classified: list[ClassifiedBlock],
) -> list[ClassifiedBlock]:
    """Collapse duplicate-style body text (single paragraph or consecutive blocks)."""
    cur = [_sanitize_translate_paragraph_interior(cb) for cb in classified]
    for _ in range(max(16, len(cur) + 8)):
        nxt = _dedupe_consecutive_redundant_translate_paragraphs_once(cur)
        if len(nxt) == len(cur):
            return nxt
        cur = nxt
    return cur
