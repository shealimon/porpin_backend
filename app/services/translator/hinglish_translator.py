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
from app.services.translator.openai_errors import openai_user_facing_message
from app.services.translator.openai_payload import (
    MAX_CHAT_OUTPUT_TOKENS_CAP,
    completion_token_params,
    normalize_openai_model,
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


PROMPT_TEMPLATE = """You are an expert literary translator specializing in converting English text into high-quality, natural, smooth Hinglish (Roman script only — never Devanagari or non-Latin script) suitable for engaging books.

Your goal is to produce a translation that feels effortless to read, conversational, and immersive — not formal, not robotic, not literal.

STRICT RULES (MANDATORY):
1. Language Style (MOST IMPORTANT)
Use natural, flowing Hinglish that feels like storytelling.
It should feel like someone is explaining ideas smoothly, not translating.
Avoid stiff, heavy, or overly formal sentences.
Prefer short to medium sentences for better readability.
Add natural conversational connectors (e.g., "toh", "bas", "lekin", "aur yahi", "simple hai", etc.) where needed for flow.
The reader should feel: "yeh padhna easy aur interesting hai"
2. Avoid Robotic / Literal Translation
DO NOT translate word-to-word.
Rewrite sentences to improve clarity and flow while keeping meaning EXACT.
Break long complex sentences into simpler ones if needed.
3. Keep These Words in English (DO NOT TRANSLATE)
Medical terms
Numbers (years, dates, measurements, counts)
Months (January, February, etc.)
Common/simple English words such as: ship, camp, crew, ice, plan, idea, team, food, water, system, action, result, etc.
Any word that sounds more natural in English than Hindi
4. Hindi Usage Constraint
Avoid pure Hindi / Sanskrit-heavy words
Use spoken, natural Hinglish
Example:
❌ "vichaar kar raha tha"
✅ "soch raha tha"
5. Formatting Rules
Preserve original paragraph structure EXACTLY
Preserve ONLY existing headings and make them bold
DO NOT add new headings
Maintain proper spacing between sentences
DO NOT use bullet points unless present in original
6. Tone & Engagement (CRITICAL)
Maintain storytelling tone and emotional depth
Make it engaging and easy to follow
Avoid textbook-style writing
Add slight natural emphasis where needed (without changing meaning)
7. Quotes Handling
Translate all quotes into natural Hinglish
Make dialogues feel human and real, not translated
8. Accuracy (NON-NEGOTIABLE)
DO NOT summarize
DO NOT skip anything
DO NOT add interpretation
Meaning must remain EXACT
9. Consistency
Keep terminology consistent across the text
10. Full coverage (no mixed English paragraphs)
Every sentence in SOURCE TEXT must be rewritten into natural Hinglish (Roman). Do not leave any full sentence in English.
Sprinkling allowed English words inside Hinglish (rule 3) is fine; standalone English sentences or clauses are not.

OUTPUT:

Return ONLY the Hinglish translation.
No explanations. No extra text.

Write in a way that a smart friend is explaining deep ideas in the simplest, most engaging way possible.

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
    prompt = PROMPT_TEMPLATE.format(chunk=sanitize_user_text(chunk))
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
                    messages=[
                        {"role": "user", "content": prompt},
                    ],
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
        return content
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
