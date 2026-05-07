"""Multi-signal heading refinement after PDF/DOCX extraction.

Combines weak lexical cues, typography / layout (``PdfLineHints``), paragraph-neighbor
context, and shallow cross-document patterns. Does **not** rely on keywords alone:
ambiguous labels (e.g. ``Judgment``, ``Notes``) need supporting typography or a
following title-like line before promotion.
"""

from __future__ import annotations

import re
from typing import Sequence

from app.models.document_models import BlockType, ContentBlock, PdfLineHints, StructuralTag
from app.services.formatter.chapter_heading_policy import chapter_like_heading_text
from app.utils.translate_filter import count_words

_NORMALIZE_WS = re.compile(r"\s+")

_FRONT_MATTER_LABELS = frozenset(
    {
        "title page",
        "copyright",
        "dedication",
        "epigraph",
        "table of contents",
        "contents",
        "table of content",
        "foreword",
        "preface",
        "introduction",
        "prologue",
        "acknowledgments",
        "acknowledgements",
    }
)

_ACADEMIC_SECTION_LABELS = frozenset(
    {
        "abstract",
        "objective",
        "methodology",
        "methods",
        "materials and methods",
        "discussion",
        "results",
        "conclusion",
        "conclusions",
        "references",
        "reference",
        "appendix",
        "bibliography",
        "glossary",
        "index",
    }
)

_SUBHEADING_LABELS = frozenset(
    {
        "summary",
        "key takeaways",
        "main idea",
        "interpretation",
        "analysis",
        "example",
        "case study",
        "story",
        "judgment",
        "judgement",
        "exercise",
        "exercises",
        "notes",
        "tips",
        "warning",
        "important",
        "remember",
        "overview",
        "background",
        "highlights",
        "discussion questions",
        "action steps",
        "final thoughts",
        "observance of the law",
        "reversal",
    }
)

_AMBIGUOUS_SINGLE_WORDS = frozenset(
    {
        "judgment",
        "judgement",
        "interpretation",
        "conclusion",
        "analysis",
        "summary",
        "notes",
        "note",
        "warning",
        "important",
        "remember",
        "example",
        "story",
        "exercise",
        "introduction",
        "abstract",
        "results",
        "discussion",
        "index",
        "references",
        "principle",
        "scene",
    }
)

_MULTI_SENT = re.compile(r"""[.!?…]["'\u201d\u2019)\]]*\s+[A-Z\u00c0-\u1fff]""")

_NUMERIC_SECTION = re.compile(
    r"(?i)^(chapter|part|book|lesson|law|principle|narrative|unit|lecture|"
    r"episode|module|segment|act)\s+(\d+|[ivxlcdm]+)\b"
)
_SECTION_MAJOR_NUMBER = re.compile(r"(?i)^section\s+\d{1,3}\b(?!\.\d)")


def _norm(s: str | None) -> str:
    if not s:
        return ""
    return _NORMALIZE_WS.sub(" ", s.strip()).lower()


def _letters_upper_ratio(text: str) -> float:
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return 0.0
    return sum(1 for c in letters if c.isupper()) / len(letters)


def _following_line_supports_heading(next_b: ContentBlock | None) -> bool:
    """Next block looks like a subtitle / section opener (validates ambiguous one-word titles)."""
    if not next_b or not next_b.text:
        return False
    t = next_b.text.strip()
    if count_words(t) > 14 or len(t) > 140:
        return False
    if not t[:1].isalpha() or not t[0].isupper():
        return False
    if t.endswith("."):
        return False
    return True


def heading_subtitle_pair_supported(next_block: ContentBlock | None) -> bool:
    """Public alias for templates / enrichment that respect two-line section openers."""
    return _following_line_supports_heading(next_block)


# Published for semantic-enrichment guards (keep in sync with confidence penalties).
AMBIGUOUS_HEADING_LEMMAS = _AMBIGUOUS_SINGLE_WORDS


def _typography_score(text: str, hints: PdfLineHints | None) -> float:
    score = 0.0
    ur = _letters_upper_ratio(text)
    if ur >= 0.88:
        score += 0.38
    elif ur >= 0.65:
        score += 0.12
    if hints:
        bf = hints.body_font_pt
        if bf is not None and hints.font_pt_max > 0:
            d = hints.font_pt_max - bf
            if d >= 7.0:
                score += 0.32
            elif d >= 3.25:
                score += 0.22
            elif d >= 1.5:
                score += 0.08
        if hints.bold_fraction >= 0.88:
            score += 0.18
        elif hints.bold_fraction >= 0.72:
            score += 0.12
        gb = hints.gap_before_pt
        if gb is not None and gb >= 14.0:
            score += 0.14
        elif gb is not None and gb >= 8.0:
            score += 0.08
        if hints.lines_merged == 1 and count_words(text) <= 12:
            score += 0.06
    wc = count_words(text)
    if wc <= 4 and len(text) <= 52 and not text.rstrip().endswith((",", ";")):
        score += 0.1
    return min(score, 1.0)


def _neighbor_title_boost(next_b: ContentBlock | None) -> float:
    if not _following_line_supports_heading(next_b):
        return 0.0
    t = next_b.text or ""
    return 0.12 if count_words(t.strip()) >= 2 else 0.08


def _lexical_score(text: str, doc_kind: str) -> float:
    t = " ".join(text.split()).strip()
    if not t:
        return 0.0
    n = _norm(t)
    if chapter_like_heading_text(t):
        return 1.0
    if _NUMERIC_SECTION.match(t) or _SECTION_MAJOR_NUMBER.match(t):
        return 0.95
    if n in _FRONT_MATTER_LABELS:
        return 0.82
    if n in _ACADEMIC_SECTION_LABELS:
        if doc_kind in ("educational", "article"):
            return 0.72
        return 0.52
    if n in _SUBHEADING_LABELS:
        return 0.5
    if doc_kind in ("self_help", "general", "book") and n in (
        "observance of the law",
        "action steps",
        "final thoughts",
        "key takeaways",
    ):
        return 0.55
    if re.match(r"(?i)^(scene|interlude|epilogue)\b", t):
        return 0.76
    return 0.0


def _negative_score(
    text: str,
    prev: ContentBlock | None,
    next_b: ContentBlock | None,
    typography: float,
) -> float:
    pen = 0.0
    t = text.strip()
    wc = count_words(t)
    ur = _letters_upper_ratio(t)
    if ur >= 0.88 and wc >= 10:
        pen += 0.4
    if wc > 24:
        pen += 0.45
    if len(t) > 180:
        pen += 0.25
    if _MULTI_SENT.search(t) and wc > 10:
        pen += 0.55
    if t.endswith(",") or t.endswith(";"):
        pen += 0.22
    if "," in t and wc >= 8 and not chapter_like_heading_text(t):
        pen += 0.18
    words = _norm(t).split()
    if len(words) == 1 and words[0] in _AMBIGUOUS_SINGLE_WORDS:
        if not _following_line_supports_heading(next_b) and typography < 0.34:
            pen += 0.32
        elif typography < 0.16:
            pen += 0.12
    if len(words) == 2 and words[0] in ("the", "a", "an"):
        pen += 0.26
    if prev and prev.type == BlockType.PARAGRAPH and prev.text and wc <= 4:
        p = prev.text.rstrip()
        if p and p[-1] not in ".!?…؟" and len(p) > 24:
            pen += 0.28
    if next_b and next_b.type == BlockType.PARAGRAPH and next_b.text:
        nxt = next_b.text.lstrip()
        if nxt and nxt[0].islower() and wc <= 3:
            pen += 0.35
    return min(pen, 1.0)


def _confidence(
    text: str,
    hints: PdfLineHints | None,
    prev: ContentBlock | None,
    next_b: ContentBlock | None,
    doc_kind: str,
) -> float:
    lex = _lexical_score(text, doc_kind)
    typ = min(1.0, _typography_score(text, hints) + _neighbor_title_boost(next_b))
    neg = _negative_score(text, prev, next_b, typ)
    raw = 0.22 + 0.5 * lex + 0.42 * typ - 0.5 * neg
    if lex >= 0.95:
        raw += 0.1
    return max(0.0, min(1.0, raw))


def _infer_document_kind(blocks: Sequence[ContentBlock]) -> str:
    """Coarse genre hint for weighting academic vs narrative labels (not exported)."""
    parts: list[str] = []
    chapters = 0
    for b in blocks:
        if b.text and b.type != BlockType.TABLE:
            parts.append(b.text.lower())
        if b.type == BlockType.HEADING and b.text and chapter_like_heading_text(b.text):
            chapters += 1
        if len(parts) >= 80:
            break
    blob = "\n".join(parts)
    if re.search(r"(?i)\b(gospel|corinthians|psalms?|qur'an|quran|vedas|torah)\b", blob):
        return "religious"
    if re.search(r"(?i)\b(biography|memoir)\s+of\b|autobiograph", blob):
        return "biography"
    if re.search(r"(?i)\b(transcript|speaker\s*:|moderator\s*:)\b", blob):
        return "transcript"
    if re.search(r"(?i)\b(abstract|methodology|doi:|issn|peer-reviewed)\b", blob):
        return "article"
    if re.search(r"(?i)\b(jee|neet|worksheet|lesson\s*plan|course\s+outline)\b", blob):
        return "educational"
    if re.search(r"(?i)\b(self[- ]help|personal growth|seven\s+habits|48\s+laws)\b", blob):
        return "self_help"
    if re.search(
        r"(?i)\b(epilogue|prologue|she\s+said|he\s+said)\b",
        blob,
    ):
        return "fiction"
    if chapters >= 3:
        return "book"
    return "general"


def _structural_tag_protected(tag: StructuralTag | None) -> bool:
    return tag in (
        StructuralTag.TITLE,
        StructuralTag.AUTHOR,
        StructuralTag.TOC,
    )


def _coaching_pattern_boost_indices(blocks: Sequence[ContentBlock]) -> set[int]:
    """Boost short lines in Law → Judgment → Interpretation-style runs."""
    boost: set[int] = set()
    lowered: list[tuple[int, str]] = []
    for i, b in enumerate(blocks):
        if b.type == BlockType.TABLE or not b.text:
            continue
        if count_words(b.text) > 4:
            continue
        lowered.append((i, _norm(b.text)))
    for j in range(len(lowered) - 2):
        a, b_, c = lowered[j][1], lowered[j + 1][1], lowered[j + 2][1]
        if re.match(r"^law\s+\d+$", a) and b_ == "judgment" and c == "interpretation":
            boost.add(lowered[j][0])
            boost.add(lowered[j + 1][0])
            boost.add(lowered[j + 2][0])
    return boost


def refine_document_structure(blocks: list[ContentBlock]) -> None:
    """Mutates ``blocks`` in place by **demoting** spurious headings only.

    We do **not** promote paragraphs to headings here—outputs should not gain outline
    nodes that the source extract did not already classify as headings (DOCX/EPUB tags
    or PDF font rules). Promotion caused false headings (e.g. TOC blobs, bold labels).
    """
    if not blocks:
        return
    snap: list[ContentBlock] = list(blocks)
    n = len(snap)
    doc_kind = _infer_document_kind(snap)
    pattern_boost = _coaching_pattern_boost_indices(snap)

    for i in range(n):
        b = snap[i]
        if b.type == BlockType.TABLE or _structural_tag_protected(b.structural_tag):
            continue
        if not b.text or not b.text.strip():
            continue
        text = " ".join(b.text.split()).strip()
        prev = snap[i - 1] if i else None
        next_b = snap[i + 1] if i + 1 < n else None
        conf = _confidence(text, b.pdf_hints, prev, next_b, doc_kind)
        if i in pattern_boost:
            conf = min(1.0, conf + 0.12)
        nkey = _norm(text)
        if (
            nkey in _SUBHEADING_LABELS
            and _following_line_supports_heading(next_b)
            and count_words(text) <= 4
        ):
            conf = max(conf, 0.62)

        if b.type == BlockType.HEADING:
            if conf < 0.40 and not chapter_like_heading_text(text):
                blocks[i] = b.model_copy(
                    update={"type": BlockType.PARAGRAPH, "level": 1, "text": text}
                )
            elif (
                conf < 0.55
                and len(_norm(text).split()) == 1
                and _norm(text) in _AMBIGUOUS_SINGLE_WORDS
                and not chapter_like_heading_text(text)
            ):
                blocks[i] = b.model_copy(
                    update={"type": BlockType.PARAGRAPH, "level": 1, "text": text}
                )
