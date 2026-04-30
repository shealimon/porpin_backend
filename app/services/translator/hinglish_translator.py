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


PROMPT_TEMPLATE = """You translate English into natural Hinglish in Roman script only (never Devanagari or other non-Latin scripts).

Aim for fluent, easy-to-read Hinglish for a wide audience—not stiff, not word-for-word when that breaks meaning. Preserve the source meaning exactly; rewrite for clarity only when needed.

Easy English mix: prefer how people actually talk—lots of common English words Indians already use (e.g. problem, time, start, idea, important, change, system, result, question, understand, help, need, feel, think, right, wrong) woven with light Hindi-in-Roman. Do not reach for formal, literary, or “pure” Hindi or heavy Sanskrit words when a simpler English word or a shorter Roman phrase is clearer.

Hard rule — no “pure” or showy Hindi: if an average city reader would more easily grasp an English word than a formal Hindi synonym, use the English word. Avoid literary, news-anchor, or textbook-only vocabulary; avoid long Sanskrit compounds and rare synonyms. When you use Hindi, keep it to words people say every day in speech (simple Roman spellings). If unsure, choose the simpler, more English-heavy phrasing—never the fancier Hindi.

Avoid Sanskritized / exam-book Roman spellings (shuddh/tatsam Hindi written in Roman, often with vi-/va-/sa-/pra-/pary-/tathya-ish feel). Prefer everyday words or English—same idea, easier mouth-feel.

STRICT blacklist (do not use these words/phrases in output): tatha, evam, athva, kintu, punah/punah, prastut, pratyek, avashyak, anivarya, nirdesh, nirdharit, upyukt, uchit, upalabdh, spashtikaran, sambandhit, sambhavit, prapt, vishesh, samanya, upyog, upay, prabhav, prabhavit, pratisthit, tathy(a). If you are about to use one, rewrite with simpler Hinglish instead.

Preferred easy replacements (pick what fits context): lekin/but, ya/or, aur/and, phir/again, har/each, zaroori/needed, rule/required, bataya/guideline, set/fixed, sahi/right, milta/available, explain/clear karna, related, possible, mila/got, special, normal, use, solution, effect/impact, established/set, fact/real/sach/haqiqat.

Also prefer common Hindustani/Urdu everyday words (when natural) over Sanskritized ones: lekin, kyunki, shayad, bilkul, haan, nahi, sach, haqiqat, fayda, nuksan, mushkil, aasaan, zaroori.

Illustrative swaps (not exhaustive; follow this spirit everywhere):
• manushya → insaan / human / people
• vichar / vicharon → soch / thoughts / idea
• sadaiv → hamesha / always
• vistarit → detail mein / detailed / in depth (not “vistarit”)
• vipreet → opposite / ulta
• vastav / vastavik → haqiqat / actually / in reality / real
• udaharan → example / jaise
• vartaman → present / abhi / aaj kal / current

Never prefer the left-column style above when the right side reads more natural for a general reader.

Keep in English (do not translate): medical terms; numbers, dates, measurements; month names; other proper nouns and terms that stay in English in India. Where Hindi fits, still prefer everyday spoken phrasing (e.g. "soch raha tha" not "vichaar kar raha tha")—but lean English when that is easier for the reader.

Translate dialogue and quoted speech into natural Hinglish the same way.

Do not summarize, omit, or add content. Keep terminology consistent. Every source sentence must appear as Hinglish in Roman; rule exceptions above for embedded English words only—not whole English sentences.

Structure: keep the same paragraph breaks and block boundaries as the source. Plain text only—no labels, preambles, or commentary. Do not use Markdown, HTML, or other markup in the translation.

OUTPUT: only the translation text.

SOURCE TEXT:
{chunk}"""

HINDI_PROMPT_TEMPLATE = """You translate English into simple, conversational Hindi in Devanagari script only (never Roman/Latin letters for Hindi words—use देवनागरी).

Aim for everyday Hindi that a city reader understands easily—not stiff Sanskritized news or textbook style. Preserve the source meaning; rewrite for clarity only when needed.

Prefer common spoken words; avoid heavy तत्सम / formal compounds when a simpler phrase is clearer. When an English word is widely used in India (e.g. problem, system, email), you may keep it in Latin letters inside the Hindi sentence if that reads more natural—otherwise use simple Hindi.

Illustrative spirit (prefer right column when the left feels heavy or literary):
• मानव / मनुष्य → लोग / इंसान
• विचार → सोच / ख्याल
• सदैव → हमेशा
• विपरीत → उल्टा / opposite sense in simple Hindi
• वास्तविक → असल / सच में
• उदाहरण → उदाहरण is fine; जैसे / for example also ok
• वर्तमान → अभी / आजकल

Do not summarize, omit, or add content. Keep paragraph breaks like the source. Plain text only—no Markdown or HTML.

OUTPUT: only the Hindi translation in Devanagari.

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
