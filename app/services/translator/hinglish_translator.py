"""OpenAI chunk translation into Hinglish (Roman script) with the product prompt."""

from __future__ import annotations

import logging
import random
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed

import httpx
from openai import APIError, APITimeoutError, OpenAI, RateLimitError

from app.core.pipeline_settings import get_pipeline_settings
from app.services.translation_target import HINGLISH, HINDI, normalize_translation_target
from app.services.translator.openai_errors import openai_user_facing_message
from app.services.translator.openai_payload import (
    MAX_CHAT_OUTPUT_TOKENS_CAP,
    completion_token_params,
    normalize_openai_model,
    sanitize_translated_output,
    sanitize_user_text,
    temperature_kw,
)

logger = logging.getLogger(__name__)

# Limits concurrent OpenAI calls process-wide so nested pools (parallel blocks ×
# chunk pools) cannot exceed translate_max_concurrency.
_api_slot_sem: threading.Semaphore | None = None
_api_slot_lock = threading.Lock()


def _api_slot_sem_get() -> threading.Semaphore:
    global _api_slot_sem
    with _api_slot_lock:
        if _api_slot_sem is None:
            n = max(1, get_pipeline_settings().translate_max_concurrency)
            _api_slot_sem = threading.Semaphore(n)
        return _api_slot_sem


HINGLISH_SYSTEM_MESSAGE = (
    "You are an expert literary translator: English → high-quality natural Hinglish in Roman "
    "(Latin) script for engaging books. Output must use only Latin letters—never Devanagari, "
    "never other non-Latin scripts. Prefer everyday Indian English plus casual Hindi spelled "
    "in Roman; never formal, Sanskritized, or bookish Hindi—even if spelled in Roman "
    '(e.g. say "soch raha tha", never "vichaar kar raha tha"). '
    "Preserve every fact exactly: years, counts, and English spelled-out numbers must not change "
    "value (no rounding, wrong decade, or wrong magnitude). "
    'For spelled-out quantities in prose, default format is Arabic numerals plus English units '
    '("22 years", "75 thousand", "3 million"), optionally with natural Hinglish glue like '
    '"se zyada" / "se kam"; do NOT replace them with Hindi/Urdu number words (wrong: '
    '"bees saal" for twenty-two; wrong: "pachaas hazaar" for seventy-five thousand). '
    "When in doubt, keep digits—never guess a Hindi/Urdu numeral phrase. "
    "Follow the user message rules exactly. Reply with only the translation—no preamble, notes, "
    "or stray tokens like Assistant or to=JSON code."
)


PROMPT_TEMPLATE = """You are an expert Hinglish book translator.

Translate the English below into smooth, natural, highly readable Hinglish in Roman script.

IMPORTANT: The output should feel like original storytelling in Hinglish—not a literal translation.

STRICT RULES:

1) Language style
- Natural conversational Hinglish; smooth, immersive sentence flow.
- Avoid robotic, word-for-word translation; split or lightly reorder long sentences only when it helps clarity—meaning must stay exact.
- Avoid overly formal Hindi and Sanskrit-heavy or pure Hindi words.
- Blend easy Hindi + easy Urdu loanwords + commonly used English words the way educated Indian readers actually read.
- Roman Hindi must sound SPOKEN, not literary: BAD vichaar kar raha tha → GOOD soch raha tha; BAD kintu, athva → GOOD lekin, ya. Unsure between heavy Hindi and simple English → choose simple English.
- Use light connectors where they help: toh, bas, lekin, matlab, phir, waise, etc.
- Section lines and soft intros (e.g. “A note from the author”): say it the way an editor would in modern Hinglish—warm and idiomatic. Avoid stiff calques and odd word order (bad: “Author se Ek Note”; prefer a natural rewrite or a light English phrase).

2) Do NOT translate these—leave in English (script/spelling as in source where sensible)
A) Numbers & numeric data: 10, 25%, 3x, 1000, page numbers, etc.
   Mandatory style for English spelled-out counts in narrative: rewrite to Arabic numerals + English scale/unit words + light Hinglish only where the sentence needs it—never Hindi/Urdu number-words for the digits.
   Examples (follow this pattern for all similar cases):
   - “twenty-two years” → “22 years” (not “baais saal”, not “bees saal”).
   - “more than seventy-five thousand people” → “75 thousand se zyada …” or keep “more than 75 thousand …”—do NOT write “pachaas hazaar” or any other wrong magnitude.
   - Same for hundreds, millions, percentages: digits + English words; add “se zyada/se kam/tak” in Roman Hindi when the source uses “more than / less than / up to”.
   Do not answer English spelled-out numbers with Hindi/Urdu number vocabulary (bees, tees, pachaas, hazaar, …); always prefer the digit form above.
B) Dates: e.g. 12 January 2025, 5th August.
C) Month names: January, February, …
D) Day names: Monday, Tuesday, …
E) Times: 10:45 PM, 6 AM, 24/7.
F) Currency & symbols: $, ₹, €, £, USD, INR, …
G) Medical terms: e.g. Depression, Anxiety, Trauma, PTSD, Diabetes, Dopamine.
H) Tech & internet: Login, Password, API, Backend, Frontend, Database, AI, Server, Email, …
I) Business / self-help: Leadership, Marketing, Networking, Mindset, Discipline, Strategy, Branding, Productivity, resilient, creative, optimistic, courageous, breakthrough, peak performance (and similar)—keep English when they carry the professional/self-help register.
J) Proper nouns (NEVER translate): person names; brand, company, app, product names; cities; countries; full book/publication titles as named (e.g. “The Alter Ego Effect”).
K) Measurements: km, kg, GB, MB, km/h, feet, inches, …
L) Abbreviations & acronyms: CEO, MBA, IIT, UPSC, GDP, UI/UX, SaaS, …
M) Scientific / academic: Algorithm, DNA, Protein, Quantum Physics, Neuroscience, …
N) Social media: Reel, Story, Followers, Subscribers, Viral, Feed, …
Also keep common Indian-English anchors when natural: problem, time, start, idea, important, change, system, result, question, understand, help, need, feel, think, right, wrong, team, plan—and similar.

3) Numbers & facts (non-negotiable)
- Same arithmetic as the source always: no rounding, no wrong magnitude or decade.
- Default for English spelled-out counts: digits + English units/scales (and Hinglish particles like “se zyada” if needed)—never approximate with Hindi/Urdu number vocabulary. A wrong number is worse than a slightly English-looking number.

4) If a Hindi (or Hinglish) choice sounds robotic, unnatural, outdated, textbook-like, or hard to read—keep the English word instead.

5) Translation quality
- Preserve emotional tone and meaning; dialogues natural; very high readability.
- Preserve paragraph breaks and sentence spacing; do not add headings or bullets unless the source has them.
- Do not add interpretation or explanation; keep structure intact.

Style mix: roughly 70–80% simple English, 20–30% light conversational Roman Hindi for flow; 0% exam-register or shuddh literary Hindi.

6) Structure / formatting
- Preserve original paragraph breaks and block boundaries EXACTLY.
- Do NOT add new headings or labels. If the source already has a title/heading line, keep it in the same role (plain text only—no Markdown; the pipeline strips **).
- Do NOT use bullet points unless the source uses them.

7) Tone & quotes
- Storytelling warmth and emotional depth; dialogues and quotes in the same natural Hinglish—human, not translated-sounding.

8) Accuracy (non-negotiable)
- Do NOT summarize, skip, omit, or add interpretation. Coverage must be complete; meaning must stay EXACT.

9) Consistency
- Keep terminology consistent across the piece.

10) Mixed English + no echo / duplication (VERY IMPORTANT)
- Translate the FULL excerpt end-to-end. Do not leave plain English intact for some clauses and heavily rewrite neighbouring clauses—it looks half-finished.
- Do not bolt English on AGAIN after an equivalent Hindi/Hinglish wording (avoid “thinking ko soch…” / doubling the same idea in both languages unless people really say both aloud).
- Say each idea ONCE with one natural wording; strip redundant English scaffolding.

11) Goal
Modern Hinglish storytelling—easy to read, emotionally engaging, natural for Indian readers, premium human translation.

OUTPUT: return ONLY the Hinglish translation. No explanations or extra text.

SOURCE TEXT:
{chunk}"""


def openai_messages_for_target(translation_target: str, user_content: str) -> list[dict[str, str]]:
    if normalize_translation_target(translation_target) == HINDI:
        return [{"role": "user", "content": user_content}]
    return [
        {"role": "system", "content": HINGLISH_SYSTEM_MESSAGE},
        {"role": "user", "content": user_content},
    ]


HINDI_PROMPT_TEMPLATE = """You translate English into simple, natural, conversational Hindi in Devanagari script only (never Roman/Latin letters for Hindi words—use देवनागरी).

Goal: The output should feel like a human is naturally explaining or narrating the content in easy Hindi. It should be smooth, effortless to read, and enjoyable—like a story. The reader should never feel they are reading a translation.

Core Style Rules:

• Use simple, everyday Hindi that people actually speak in daily life.
• Avoid formal, bookish, or news-style Hindi.
• Prefer easy and familiar words over complex or heavy ones.

• If a sentence sounds stiff, rewrite it to make it more natural and clear—without changing meaning.

• You may keep commonly used English words (like problem, system, email, idea) in Latin script if that feels more natural and improves readability.

Strict Language Rules (VERY IMPORTANT):

• Avoid heavy, Sanskritized, or textbook-style Hindi.
• Do NOT use uncommon or difficult Hindi words that people don't use in daily conversation.
• If a simpler Hindi or common English word works better, always choose that.

• If unsure between complex Hindi vs simple wording → choose the simpler option.

Flow & Readability:

• Sentences should feel smooth, connected, and natural—not literal translation.
• Add light conversational flow so it feels like someone is explaining or narrating.
• The output should be easy to read without mental effort.

• You may slightly rewrite sentences for clarity and flow, but do NOT change meaning.

Strict Avoid:

• Literal word-by-word translation
• Robotic or stiff tone
• Overly formal or heavy Hindi
• Complex or uncommon vocabulary

Style Target:

• Easy, spoken Hindi (primary)
• Light mix of commonly used English words where natural
• 100% smooth, story-like flow

Structure Rules:

• Preserve original structure exactly as it is
• If the source text contains a chapter title, heading, or section label, keep it and translate it naturally into Hindi
• Do NOT remove, modify, or add new headings
• If no heading exists in the source, do NOT create one
• Do not add or remove content
• Keep paragraph breaks the same as the source

OUTPUT: only the Hindi translation in Devanagari script.

SOURCE TEXT:
{chunk}"""


def translate_chunk(
    client: OpenAI,
    model: str,
    chunk: str,
    *,
    on_tokens: Callable[[int], None] | None = None,
) -> str:
    if not chunk.strip():
        return chunk
    safe = sanitize_user_text(chunk)
    # Use replace, not str.format — source text may contain `{...}` (JSON, placeholders).
    prompt = PROMPT_TEMPLATE.replace("{chunk}", safe)
    settings = get_pipeline_settings()
    max_retries = settings.gpt_max_retries
    last_err: Exception | None = None
    for attempt in range(max_retries):
        try:
            sem = (
                _api_slot_sem_get()
                if settings.translate_use_process_wide_slot_sem
                else None
            )
            if sem is not None:
                sem.acquire()
            try:
                resp = client.chat.completions.create(
                    model=model,
                    messages=openai_messages_for_target(HINGLISH, prompt),
                    **temperature_kw(model, float(settings.translation_temperature)),
                    **completion_token_params(model, MAX_CHAT_OUTPUT_TOKENS_CAP),
                )
            finally:
                if sem is not None:
                    sem.release()
        except (RateLimitError, APITimeoutError) as e:
            last_err = e
            wait = min(60.0, (2**attempt) + random.random())
            logger.warning(
                "OpenAI transient error (attempt %s/%s): %s; sleeping %.1fs",
                attempt + 1,
                max_retries,
                e,
                wait,
            )
            time.sleep(wait)
            continue
        except APIError as e:
            last_err = e
            if getattr(e, "status_code", None) == 429:
                wait = min(60.0, (2**attempt) + random.random())
                time.sleep(wait)
                continue
            raise RuntimeError(openai_user_facing_message(e)) from e
        choice = resp.choices[0]
        content = (choice.message.content or "").strip()
        usage = getattr(resp, "usage", None)
        if usage is not None:
            total = int(getattr(usage, "total_tokens", None) or 0)
            if total and on_tokens is not None:
                on_tokens(total)
            logger.debug(
                "Chunk translation tokens: total=%s prompt=%s completion=%s",
                getattr(usage, "total_tokens", None),
                getattr(usage, "prompt_tokens", None),
                getattr(usage, "completion_tokens", None),
            )
        if not content:
            logger.warning("Empty translation for chunk; echoing source")
            return chunk
        return sanitize_translated_output(content)
    _msg = (
        f"OpenAI failed after {max_retries} attempts: "
        f"{openai_user_facing_message(last_err) if last_err else 'unknown'}"
    )
    if last_err is not None:
        raise RuntimeError(_msg) from last_err
    raise RuntimeError(_msg)


def _openai_client() -> OpenAI:
    settings = get_pipeline_settings()
    return OpenAI(
        api_key=settings.openai_api_key,
        timeout=httpx.Timeout(180.0, connect=20.0),
        max_retries=0,
    )


def translate_chunks(
    chunks: list[str],
    *,
    after_each: Callable[[], None] | None = None,
    on_tokens: Callable[[int], None] | None = None,
) -> list[str]:
    settings = get_pipeline_settings()
    if not settings.openai_api_key:
        raise RuntimeError("OPENAI_API_KEY is not configured")
    model = normalize_openai_model(settings.openai_model)
    if not chunks:
        return []

    tok_lock = threading.Lock()

    def bump_tokens(n: int) -> None:
        if on_tokens is None:
            return
        with tok_lock:
            on_tokens(n)

    workers = min(settings.translate_max_concurrency, len(chunks))
    if workers <= 1:
        client = _openai_client()
        out: list[str] = []
        for c in chunks:
            out.append(translate_chunk(client, model, c, on_tokens=bump_tokens))
            if after_each is not None:
                after_each()
        return out

    def run_one(idx: int, text: str) -> tuple[int, str]:
        return idx, translate_chunk(
            _openai_client(), model, text, on_tokens=bump_tokens
        )

    results: list[str | None] = [None] * len(chunks)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = [pool.submit(run_one, i, c) for i, c in enumerate(chunks)]
        for fut in as_completed(futs):
            idx, translated = fut.result()
            results[idx] = translated
            if after_each is not None:
                with tok_lock:
                    after_each()

    out = [results[i] for i in range(len(chunks))]
    if any(x is None for x in out):
        raise RuntimeError("Incomplete parallel translation batch")
    return out
