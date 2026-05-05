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
    "Follow the user message rules exactly. Reply with only the translation—no preamble, notes, "
    "or stray tokens like Assistant or to=JSON code."
)


PROMPT_TEMPLATE = """Translate the English below into natural, smooth Hinglish in Roman script.

Goal: effortless to read, conversational, immersive storytelling—not formal, not robotic, not literal. Prefer short-to-medium sentences. The reader should feel yeh padhna easy aur interesting hai.

STRICT RULES:

1) Language style (most important)
- Flowing Hinglish: ideas explained smoothly, like someone talking, not translating.
- Avoid stiff, heavy, or textbook tone.
- Use light connectors where they help: toh, bas, lekin, aur yahi, simple hai, matlab, phir, waise, etc.

2) Not robotic / literal
- Do NOT translate word-for-word. Rewrite for clarity and flow while keeping meaning EXACT.
- Break long, complex sentences when it helps.

3) Keep in English (do NOT translate)
- Medical terms.
- Numbers: years, dates, measurements, counts.
- Month names (January, February, etc.).
- Common/simple English Indians leave in English when speaking: ship, camp, crew, ice, plan, idea, team, food, water, system, action, result, problem, time, change, important, question, understand, help, need, feel, think, right, wrong, start—and similar words.
- Any word that sounds more natural in English than a Hindi replacement.

4) Hindi usage constraint (Roman must sound SPOKEN, not literary)
- Avoid pure Hindi, Sanskrit-heavy, or exam/news bookish words—even in Roman spelling.
- Every Hindi-flavored word must be what people actually say aloud, not shuddh/literary forms.
Examples:
- BAD: vichar / vichaar kar raha tha → GOOD: soch raha tha
- BAD: kintu, athva, apeksha → GOOD: lekin, ya, umeed (only if natural in context)
- If unsure between a heavy Hindi term and simple English → choose simple English.

Style mix: roughly 70–80% simple English, 20–30% light conversational Roman Hindi for flow; 0% pure/formal Hindi register.

5) Structure / formatting
- Preserve original paragraph breaks and block boundaries EXACTLY.
- Do NOT add new headings or labels. If the source already has a title/heading line, keep it in the same role (plain text only—no Markdown; the pipeline strips **).
- Do NOT use bullet points unless the source uses them. Maintain normal spacing between sentences.

6) Tone & quotes
- Storytelling warmth and emotional depth; dialogues and quotes in the same natural Hinglish—human, not translated-sounding.

7) Accuracy (non-negotiable)
- Do NOT summarize, skip, omit, or add interpretation. Coverage must be complete; meaning must stay EXACT.

8) Consistency
- Keep terminology consistent across the piece.

9) Mixed English + no echo / duplication (VERY IMPORTANT)
- Translate the FULL excerpt end-to-end. Do not leave plain English intact for some clauses and heavily rewrite neighbouring clauses—it looks half-finished.
- Borrowed/simple English nouns/tools are OK only where rule (3) says; do not bolt them on AGAIN after an equivalent Hindi/Hinglish wording (avoid “thinking ko soch…” / doubling the same idea in both languages unless people really say both aloud).
- Say each idea ONCE with one natural wording; strip redundant English scaffolding.

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
