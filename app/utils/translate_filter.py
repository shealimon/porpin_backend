"""Decide which sentences are worth sending to the translation API (cost + length control)."""

from __future__ import annotations

import re
from dataclasses import dataclass


def count_words(text: str) -> int:
    return len(re.findall(r"\w+", text or "", flags=re.UNICODE))


# Avoid splitting on periods inside common academic abbreviations.
_ABBREV_MARK = "\uffff"

_ABBREV_REPLACEMENTS = (
    ("e.g.", "e.g" + _ABBREV_MARK),
    ("i.e.", "i.e" + _ABBREV_MARK),
    ("et al.", "et al" + _ABBREV_MARK),
    ("Fig.", "Fig" + _ABBREV_MARK),
    ("fig.", "fig" + _ABBREV_MARK),
    ("Dr.", "Dr" + _ABBREV_MARK),
    ("Mr.", "Mr" + _ABBREV_MARK),
    ("Mrs.", "Mrs" + _ABBREV_MARK),
    ("Ms.", "Ms" + _ABBREV_MARK),
    ("vs.", "vs" + _ABBREV_MARK),
    ("Inc.", "Inc" + _ABBREV_MARK),
    ("Ltd.", "Ltd" + _ABBREV_MARK),
)


def split_sentences(text: str) -> list[str]:
    t = text or ""
    for a, b in _ABBREV_REPLACEMENTS:
        t = t.replace(a, b)
    parts = re.split(r"(?<=[.!?])\s+", t)
    out: list[str] = []
    for p in parts:
        s = p.replace(_ABBREV_MARK, ".").strip()
        if s:
            out.append(s)
    return out


_REF_YEAR = re.compile(r"\b(19|20)\d{2}\b")
# Sentence-ending punctuation before optional closing quotes/brackets (reuse for merge heuristics).
_SENTENCE_COMPLETE_END = re.compile(
    r"""[.!?…।]['"\u201d\u2019)\]]*\s*$""",
    flags=re.UNICODE,
)


def sentence_appears_complete(text: str) -> bool:
    """True when trailing text looks like end of sentence (paragraph merge / planning)."""
    t = (text or "").strip()
    if not t:
        return True
    if t.endswith("..."):
        return True
    return bool(_SENTENCE_COMPLETE_END.search(t))


def first_non_space_char(s: str) -> str | None:
    """First non-whitespace grapheme-ish char, or None if empty."""
    for c in (s or ""):
        if not c.isspace():
            return c
    return None


def looks_like_sentence_continuation_line(text: str) -> bool:
    """Next line clearly continues mid-sentence (do not merge as new paragraph after a period)."""
    c = first_non_space_char(text or "")
    if c is None:
        return False
    if not c.isalpha():
        return True
    return c.islower()


_STANDALONE_SECTION_LABEL = re.compile(
    r"""(?ix)^(?:
        preface | introduction | foreword | prologue | epilogue |
        acknowledg(?:e)?ments? | dedication |
        contents? | table\ of\ contents |
        (?:part|book)\s+[ivxlcdm\d]+ |
        chapter\s+\d+ |
        appendix\s+[a-z0-9]+ |
        notes? | references? | bibliography | glossary | index
    )\s*$"""
)


def looks_like_standalone_section_label(text: str) -> bool:
    """Single-line front/back matter labels without final punctuation (often PDF shards).

    They must not be merged into the following paragraph: ``merge_adjacent_translate_paragraphs``
    treats no trailing ``.?!`` as incomplete and would glue the next block.
    """
    t = " ".join((text or "").split()).strip()
    if not t or len(t) > 120:
        return False
    if _STANDALONE_SECTION_LABEL.match(t):
        return True
    return False


_BRACKET_NUM = re.compile(r"\[[\d,\s–-]+\]")
_URL = re.compile(r"https?://|www\.", re.I)
_FIG_TABLE_SHORT = re.compile(
    r"^(figure|fig\.?|table)\s+\d+([.:)\s]|$).{0,40}$", re.I
)


@dataclass(frozen=True)
class SentencePlan:
    text: str
    send_to_api: bool


def _numeric_or_symbol_heavy(s: str) -> bool:
    words = re.findall(r"\w+|[^\w\s]", s, flags=re.UNICODE)
    if not words:
        return True
    alpha_words = [w for w in words if re.match(r"^\w+$", w) and re.search(r"[A-Za-z]", w)]
    return len(alpha_words) < 2 and len(s) > 8


def _citation_or_reference_heavy(s: str) -> bool:
    if _URL.search(s):
        return True
    if "doi:" in s.lower() or "arxiv:" in s.lower() or "isbn" in s.lower():
        return True
    if "©" in s or "(c)" in s.lower():
        return True
    brackets = len(_BRACKET_NUM.findall(s))
    years = len(_REF_YEAR.findall(s))
    if brackets >= 2 or (brackets >= 1 and years >= 1 and len(s) < 400):
        return True
    if years >= 3 and len(s) < 500:
        return True
    if "et al" in s.lower() and years >= 1:
        return True
    return False


def _should_skip_sentence_heuristic(sentence: str) -> bool:
    s = (sentence or "").strip()
    if not s:
        return True
    if count_words(s) <= 1 and len(s) < 30:
        return True
    if _FIG_TABLE_SHORT.match(s.strip()):
        return True
    if _citation_or_reference_heavy(s):
        return True
    if _numeric_or_symbol_heavy(s):
        return True
    return False


def _apply_word_budget(plans: list[SentencePlan], max_api_word_ratio: float) -> list[SentencePlan]:
    """If too much text would still hit the API, skip extra low-value sentences (order preserved)."""
    if max_api_word_ratio >= 1.0 or max_api_word_ratio <= 0:
        return plans
    total_w = sum(count_words(p.text) for p in plans)
    if total_w == 0:
        return plans

    def api_words(ps: list[SentencePlan]) -> int:
        return sum(count_words(p.text) for p in ps if p.send_to_api)

    target = int(total_w * max_api_word_ratio) + 1
    if api_words(plans) <= target:
        return plans

    # Score sentences we are still sending: drop shortest / most Latin-only first.
    indices = [i for i, p in enumerate(plans) if p.send_to_api]
    scored: list[tuple[int, int, int]] = []
    for i in indices:
        p = plans[i]
        w = count_words(p.text)
        non_ascii = len(re.findall(r"[^\x00-\x7F]", p.text))
        scored.append((w, -non_ascii, i))
    scored.sort()

    mutable = [SentencePlan(p.text, p.send_to_api) for p in plans]
    for w, _na, i in scored:
        if w == 0:
            continue
        if api_words(mutable) <= target:
            break
        mutable[i] = SentencePlan(mutable[i].text, False)
    return mutable


def plan_paragraph_for_translation(
    paragraph: str,
    *,
    max_api_word_ratio: float,
) -> list[SentencePlan]:
    """
    Split a paragraph into sentences and mark which ones should be translated via the API.
    Others are returned verbatim (no GPT) to save cost and avoid inflated phrasing on boilerplate.

    When ``max_api_word_ratio >= 1.0``, per-sentence skipping is disabled so the paragraph is sent
    to the translator as one unit—avoids verbatim English stitched next to translated text.
    """
    para = (paragraph or "").strip()
    if not para:
        return []
    # Full-literary translation: whole paragraph goes to GPT (recommended for books at ratio 1.0).
    if max_api_word_ratio >= 1.0:
        return [SentencePlan(para, True)]

    sents = split_sentences(para)
    if not sents:
        return [SentencePlan(para, not _should_skip_sentence_heuristic(para))]

    plans = [
        SentencePlan(s, not _should_skip_sentence_heuristic(s)) for s in sents
    ]
    return _apply_word_budget(plans, max_api_word_ratio)
