"""High-throughput translation: many segments per OpenAI request + asyncio concurrency."""

from __future__ import annotations

import asyncio
import json
import logging
import random
import re
import time
from collections.abc import Callable

import httpx
from openai import APIError, APITimeoutError, AsyncOpenAI, RateLimitError

from app.core.pipeline_settings import get_pipeline_settings
from app.services.translator.hinglish_translator import (
    HINDI_PROMPT_TEMPLATE,
    PROMPT_TEMPLATE,
)
from app.services.translation_target import HINDI, normalize_translation_target
from app.services.translator.openai_errors import openai_user_facing_message
from app.services.translator.openai_payload import (
    batch_segments_json_schema_response_format,
    completion_token_params,
    finite_temperature,
    model_supports_response_format_json_object,
    model_supports_structured_outputs_json_schema,
    normalize_openai_model,
    sanitize_translated_output,
    sanitize_user_text,
    temperature_kw,
)
from app.utils.chunking import count_tokens

logger = logging.getLogger(__name__)

_JSON_FENCE = re.compile(r"```(?:json)?\s*([\s\S]*?)```", re.I)
_OPENAI_TRY_AGAIN_MS = re.compile(r"try again in ([\d.]+)\s*ms", re.I)
_SEGMENT_INDEX_KEY = re.compile(r"^(\d+)$", re.I)
_SEGMENT_PREFIX_KEY = re.compile(r"^segment_(\d+)$", re.I)
_KEY_FUZZY_INDEX = re.compile(
    r"^(?:seg(?:ment)?|s|t|idx|i|n|key|k)[_\-](\d+)$",
    re.I,
)
_WRAPPER_KEYS = frozenset(
    {
        "translations",
        "segments",
        "data",
        "results",
        "output",
        "items",
        "response",
        "segments_out",
        "payload",
        "body",
        "segment_translations",
        "translations_by_index",
    }
)
_NESTED_VALUE_KEYS = (
    "text",
    "translation",
    "hinglish",
    "content",
    "value",
    "t",
    "translated",
)
_LIST_ITEM_ID_KEYS = frozenset(
    {"i", "id", "index", "idx", "n", "segment", "seg", "k", "key", "segment_id"}
)
_LIST_ITEM_TEXT_KEYS = frozenset(_NESTED_VALUE_KEYS) | {
    "en",
    "src",
    "dst",
    "output",
    "out",
    "hi",
    "target",
}

# gpt-4o / gpt-4o-mini chat.completions output cap (requests above this return 400).
_MAX_COMPLETION_TOKENS = 16_384


def _completion_budget_multi(segments: list[str]) -> int:
    """Cap completion size; must stay within the model's max_completion_tokens."""
    inn = sum(count_tokens(s) for s in segments)
    n = len(segments)
    # Large n → large JSON object; truncation causes parse failures and N single-segment calls.
    structure = min(6000, 14 * n + 320)
    est = max(768, int(inn * 2.75) + 850 + structure)
    return min(_MAX_COMPLETION_TOKENS, est)


def _completion_budget_single(chunk: str) -> int:
    inn = count_tokens(chunk)
    return min(_MAX_COMPLETION_TOKENS, max(384, int(inn * 2.5) + 400))


MULTI_SEGMENT_RULES_PREFIX = """You will translate multiple labeled segments from English into Hinglish using the same rules as single-segment translation.

Roman script only (never Devanagari). Prefer common English words Indians use (e.g. problem, time, start, idea, important, change, system, result, question, understand, help, need, feel, think, right, wrong). Only convert difficult, formal, or unnatural English into simple Hinglish where needed. Natural spoken/storytelling tone; light Hindi-in-Roman for flow and relatability only—not to replace English.

Numbers: for spelled-out quantities, use Arabic numerals + English units (e.g. “22 years”; “more than seventy-five thousand” → “75 thousand se zyada” or “more than 75 thousand”). Do not use Hindi/Urdu number words for those values (wrong: “bees saal” for 22; wrong: “pachaas hazaar” for 75,000). Never change magnitude—when unsure, digits only.

Proper nouns, book titles, brands, self-help/business terms: leave in English as in the single-segment rules.

Strict: avoid pure/formal/Sanskrit-type or bookish exam/news Hindi; if a word feels uncommon in daily speech, do not use it; if unsure Hindi vs English, choose simple English. Smooth, connected sentences—not literal, robotic, or stiff; no overuse of Hindi. Target ~70–80% simple English, ~20–30% light conversational Hinglish, 0% pure Hindi/Sanskrit, natural storytelling flow. Slight rewrites for clarity/flow are ok if meaning is unchanged.

No stitched output: translate each segment wholly; avoid leaving untouched English clauses mixed with rewritten text. Do not echo the same idea twice (English+Hinglish) unless it is natural idiomatic repetition.
Never include the words Assistant, to=JSON, code, segment keys, or JSON instructions inside any translation string—the output values must be book prose only.

Structure: preserve each segment's structure; translate existing headings/titles naturally into Hinglish; do not add, remove, or invent headings. Plain text only inside each JSON string value.

Other rules: full coverage, no summarizing, no omissions.

Return one JSON object (not an array) with string keys "0" through "{last_idx}" only—one per segment, no gaps. Each value is one JSON string: the plain translation for that segment (escape quotes and newlines per JSON). Each string must be plain text only—no Markdown, HTML, or styling markup.
Output that object directly with no extra keys, no wrapper objects, and no text before or after the JSON.

Example: {"0":"...","1":"...","2":"..."}
"""

MULTI_SEGMENT_RULES_HINDI_PREFIX = """You will translate multiple labeled segments from English into simple conversational Hindi in Devanagari using the same rules as single-segment translation (everyday words, not heavy Sanskrit; full coverage, no summarizing).

Return one JSON object (not an array) with string keys "0" through "{last_idx}" only—one per segment, no gaps. Each value is one JSON string: the plain translation for that segment in Devanagari (escape quotes and newlines per JSON). Each string must be plain text only—no Markdown, HTML, or styling markup.
Never include the words Assistant, to=JSON, code, segment keys, or JSON instructions inside any translation string—Devanagari prose only.
Output that object directly with no extra keys, no wrapper objects, and no text before or after the JSON.

Example: {"0":"...","1":"...","2":"..."}
"""


def _multi_segment_rules_prefix_for_target(translation_target: str) -> str:
    return (
        MULTI_SEGMENT_RULES_HINDI_PREFIX
        if normalize_translation_target(translation_target) == HINDI
        else MULTI_SEGMENT_RULES_PREFIX
    )


def _single_segment_prompt_template(translation_target: str) -> str:
    return (
        HINDI_PROMPT_TEMPLATE
        if normalize_translation_target(translation_target) == HINDI
        else PROMPT_TEMPLATE
    )


def _openai_http_status_is_transient_server_error(status_code: int | None) -> bool:
    """HTTP 5xx from the API gateway / upstream — safe to retry with backoff."""
    if status_code is None:
        return False
    return 500 <= status_code <= 599


def _openai_retry_sleep_seconds(exc: Exception, attempt: int) -> float:
    """Prefer OpenAI's suggested delay for 429s; else exponential backoff (capped)."""
    msg = str(exc)
    m = _OPENAI_TRY_AGAIN_MS.search(msg)
    if m:
        # Floor + jitter so many parallel retries don't wake in the same millisecond.
        base_ms = float(m.group(1)) / 1000.0
        floor = 0.35 * (attempt + 1)
        return min(120.0, max(base_ms, floor) + random.random() * 0.55)
    return min(90.0, (2**attempt) + random.random())


async def _translate_segments_parallel_limited(
    client: AsyncOpenAI,
    model: str,
    segments: list[str],
    *,
    on_tokens: Callable[[int], None] | None,
    max_parallel: int,
    inflight: asyncio.Semaphore,
    translation_target: str,
) -> list[str]:
    """Run single-segment calls with a hard cap — unbounded gather exhausts TPM (429)."""
    cap = max(1, min(max_parallel, len(segments)))
    local = asyncio.Semaphore(cap)

    async def one(seg: str) -> str:
        async with local:
            return await _async_translate_one(
                client,
                model,
                seg,
                on_tokens=on_tokens,
                inflight=inflight,
                translation_target=translation_target,
            )

    return await asyncio.gather(*[one(s) for s in segments])


def _fallback_single_segment_parallelism() -> int:
    settings = get_pipeline_settings()
    # Stay well under translate_batch_max_concurrency when fanning out per-segment calls.
    return max(1, min(4, max(1, settings.translate_batch_max_concurrency // 2)))


def _prompt_for_segments(segments: list[str], *, translation_target: str) -> str:
    last_idx = max(0, len(segments) - 1)
    rules = _multi_segment_rules_prefix_for_target(translation_target).replace(
        "{last_idx}", str(last_idx)
    )
    parts = [rules, "\n\nSEGMENTS:\n"]
    for i, seg in enumerate(segments):
        parts.append(f'\n---SEGMENT_{i}---\n"""\n')
        parts.append(sanitize_user_text(seg))
        parts.append('\n"""\n')
    return "".join(parts)


def _json_segment_value_to_str(v: object) -> str | None:
    """Accept model quirks: bare numbers, occasional non-strings."""
    if v is None:
        return None
    if isinstance(v, str):
        return v
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return str(v)
    return None


def _coerce_nested_segment_value(v: object, depth: int = 0) -> str | None:
    """Segment value may be a string, nested object, or list of lines / alternatives."""
    if depth > 4:
        return None
    s = _json_segment_value_to_str(v)
    if s is not None:
        return sanitize_translated_output(s)
    if isinstance(v, list):
        parts: list[str] = []
        for item in v:
            piece = _coerce_nested_segment_value(item, depth + 1)
            if piece is not None and piece != "":
                parts.append(piece)
        if parts:
            return "\n".join(parts)
        return None
    if isinstance(v, dict):
        for nk in _NESTED_VALUE_KEYS:
            if nk in v:
                got = _coerce_nested_segment_value(v[nk], depth + 1)
                if got is not None:
                    return got
        if len(v) == 1:
            return _coerce_nested_segment_value(next(iter(v.values())), depth + 1)
    return None


def _unwrap_batch_container(obj: object) -> object:
    """Strip one-key wrappers like ``{\"translations\": {...}}``."""
    depth = 0
    while depth < 6 and isinstance(obj, dict) and len(obj) == 1:
        k = next(iter(obj.keys()))
        ks = str(k).strip().lower().replace(" ", "_")
        if ks in _WRAPPER_KEYS or ks.endswith("_translations") or ks.endswith("_segments"):
            obj = obj[k]
            depth += 1
            continue
        break
    return obj


def _dict_key_to_index(key: object) -> int | None:
    if isinstance(key, int) and not isinstance(key, bool):
        return int(key)
    if isinstance(key, str):
        s = key.strip()
        for zw in ("\u200b", "\u200c", "\u200d", "\ufeff"):
            s = s.replace(zw, "")
        s = s.strip()
        m = _SEGMENT_INDEX_KEY.match(s)
        if m:
            return int(m.group(1))
        m2 = _SEGMENT_PREFIX_KEY.match(s)
        if m2:
            return int(m2.group(1))
        m3 = _KEY_FUZZY_INDEX.match(s)
        if m3:
            return int(m3.group(1))
    return None


def _strip_trailing_commas_json(s: str) -> str:
    prev = None
    while prev != s:
        prev = s
        s = re.sub(r",\s*([}\]])", r"\1", s)
    return s


def _json_loads_lenient(raw: str) -> object | None:
    """Parse model JSON; repair trailing commas and optionally use ``json-repair`` for broken strings."""
    for candidate in (raw, _strip_trailing_commas_json(raw)):
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            continue

    try:
        from json_repair import loads as json_repair_loads

        return json_repair_loads(raw)
    except ImportError:
        logger.debug("json_repair not installed; skipping lenient JSON repair")
    except Exception:
        logger.debug("json_repair.loads failed", exc_info=True)

    for opener in ("{", "["):
        start = raw.find(opener)
        if start < 0:
            continue
        tail = raw[start:]
        for cand in (tail, _strip_trailing_commas_json(tail)):
            try:
                obj, _ = json.JSONDecoder().raw_decode(cand)
                return obj
            except json.JSONDecodeError:
                continue

    return None


def _extract_list_item_index(d: dict) -> int | None:
    for ik in _LIST_ITEM_ID_KEYS:
        if ik not in d:
            continue
        raw = d[ik]
        if isinstance(raw, int) and not isinstance(raw, bool):
            return int(raw)
        if isinstance(raw, str) and raw.strip().isdigit():
            return int(raw.strip())
    return None


def _extract_list_item_text(d: dict) -> str | None:
    for tk in _LIST_ITEM_TEXT_KEYS:
        if tk in d:
            got = _coerce_nested_segment_value(d[tk])
            if got is not None:
                return got
    if len(d) == 1:
        return _coerce_nested_segment_value(next(iter(d.values())))
    return None


def _parse_segment_list(obj: list, n: int) -> list[str] | None:
    if len(obj) < n:
        return None
    if not all(isinstance(x, dict) for x in obj):
        if len(obj) != n:
            return None
        out_list: list[str] = []
        for item in obj:
            s = _coerce_nested_segment_value(item)
            if s is None:
                return None
            out_list.append(s)
        return out_list

    remapped: dict[int, str] = {}
    positional: list[str] = []
    strict_rows = len(obj) == n
    for item in obj:
        idx = _extract_list_item_index(item)
        text = _extract_list_item_text(item)
        if text is None:
            if strict_rows:
                return None
            continue
        if idx is not None:
            remapped[idx] = text
        else:
            positional.append(text)

    if remapped and positional:
        return None
    if remapped:
        if all(i in remapped for i in range(n)):
            return [remapped[i] for i in range(n)]
        if all(i in remapped for i in range(1, n + 1)):
            return [remapped[i] for i in range(1, n + 1)]
        return None
    if len(positional) == n:
        return positional
    return None


def _extract_ordered_segments_from_dict(obj: dict, n: int) -> list[str] | None:
    """Build ``[seg0, ...]`` from a dict; ignore non-index keys; 0- or 1-based indices.

    Models often add extra top-level keys (e.g. ``"note"``) or wrap counts; require only
    that every required index is present with a string-coercible value.
    """
    remapped: dict[int, str] = {}
    for key, v in obj.items():
        idx = _dict_key_to_index(key)
        if idx is None:
            continue
        s = _coerce_nested_segment_value(v)
        if s is None:
            return None
        remapped[idx] = s

    if all(i in remapped for i in range(n)):
        return [remapped[i] for i in range(n)]
    if all(i in remapped for i in range(1, n + 1)):
        return [remapped[i] for i in range(1, n + 1)]
    return None


def _deep_find_indexed_segments(obj: object, n: int, depth: int = 0) -> list[str] | None:
    """Models often nest the real map under ``translations``, ``data``, or an extra object."""
    if depth > 6:
        return None
    if isinstance(obj, list):
        return _parse_segment_list(obj, n)
    if not isinstance(obj, dict):
        return None
    got = _extract_ordered_segments_from_dict(obj, n)
    if got is not None:
        return got
    for v in obj.values():
        if isinstance(v, (dict, list)):
            inner = _deep_find_indexed_segments(v, n, depth + 1)
            if inner is not None:
                return inner
    return None


def _parse_multi_segment_json(content: str, n: int) -> list[str] | None:
    raw = (content or "").strip()
    if not raw:
        return None
    m = _JSON_FENCE.search(raw)
    if m:
        raw = m.group(1).strip()

    obj = _json_loads_lenient(raw)
    if obj is None:
        return None

    obj = _unwrap_batch_container(obj)

    if isinstance(obj, list):
        return _parse_segment_list(obj, n)

    if not isinstance(obj, dict):
        return None

    ordered = _extract_ordered_segments_from_dict(obj, n)
    if ordered is not None:
        return ordered

    return _deep_find_indexed_segments(obj, n)


async def _async_translate_one(
    client: AsyncOpenAI,
    model: str,
    chunk: str,
    *,
    on_tokens: Callable[[int], None] | None = None,
    inflight: asyncio.Semaphore,
    translation_target: str,
) -> str:
    """Single-segment completion (same prompt as legacy ``translate_chunk``)."""
    if not chunk.strip():
        return chunk
    settings = get_pipeline_settings()
    safe_chunk = sanitize_user_text(chunk)
    temp = finite_temperature(float(settings.translation_temperature))
    tgt = normalize_translation_target(translation_target)
    if settings.translation_cache_enabled and settings.redis_url:

        def _cache_get() -> str | None:
            from app.services.translation_cache import lookup_cached_translations

            row = lookup_cached_translations(
                [safe_chunk],
                model=model,
                temperature=temp,
                translation_target=tgt,
            )
            return row[0] if row else None

        hit = await asyncio.to_thread(_cache_get)
        if hit is not None:
            return hit

    # Use replace, not str.format — source text may contain `{...}` (JSON, placeholders).
    prompt = _single_segment_prompt_template(tgt).replace("{chunk}", safe_chunk)
    max_retries = settings.gpt_max_retries
    last_err: Exception | None = None
    for attempt in range(max_retries):
        try:
            async with inflight:
                resp = await client.chat.completions.create(
                    model=model,
                    messages=[{"role": "user", "content": prompt}],
                    **temperature_kw(model, float(settings.translation_temperature)),
                    **completion_token_params(model, _completion_budget_single(safe_chunk)),
                )
        except (RateLimitError, APITimeoutError) as e:
            last_err = e
            wait = _openai_retry_sleep_seconds(e, attempt)
            logger.warning(
                "OpenAI single-segment retry (attempt %s/%s): %s; sleeping %.1fs",
                attempt + 1,
                max_retries,
                e,
                wait,
            )
            await asyncio.sleep(wait)
            continue
        except APIError as e:
            last_err = e
            sc = getattr(e, "status_code", None)
            if sc == 429 or _openai_http_status_is_transient_server_error(sc):
                wait = _openai_retry_sleep_seconds(e, attempt)
                kind = "429" if sc == 429 else f"HTTP {sc}"
                logger.warning(
                    "OpenAI single-segment retry (%s, attempt %s/%s): %s; sleeping %.1fs",
                    kind,
                    attempt + 1,
                    max_retries,
                    e,
                    wait,
                )
                await asyncio.sleep(wait)
                continue
            raise RuntimeError(openai_user_facing_message(e)) from e
        choice = resp.choices[0]
        content = (choice.message.content or "").strip()
        usage = getattr(resp, "usage", None)
        if usage is not None and on_tokens is not None:
            total = int(getattr(usage, "total_tokens", None) or 0)
            if total:
                on_tokens(total)
        if not content:
            return chunk
        content = sanitize_translated_output(content)
        if not content.strip():
            return chunk
        if settings.translation_cache_enabled and settings.redis_url:

            def _cache_put() -> None:
                from app.services.translation_cache import store_cached_translations

                store_cached_translations(
                    [safe_chunk],
                    [content],
                    model=model,
                    temperature=temp,
                    translation_target=tgt,
                )

            await asyncio.to_thread(_cache_put)
        return content
    _msg1 = (
        f"OpenAI failed after {max_retries} attempts: "
        f"{openai_user_facing_message(last_err) if last_err else 'unknown'}"
    )
    if last_err is not None:
        raise RuntimeError(_msg1) from last_err
    raise RuntimeError(_msg1)


async def _openai_translate_multi_segment(
    client: AsyncOpenAI,
    model: str,
    segments: list[str],
    *,
    on_tokens: Callable[[int], None] | None = None,
    inflight: asyncio.Semaphore,
    translation_target: str,
) -> list[str]:
    """Multi-segment OpenAI completion only (no Redis translation cache)."""
    tgt = normalize_translation_target(translation_target)
    if len(segments) == 1:
        return [
            await _async_translate_one(
                client,
                model,
                segments[0],
                on_tokens=on_tokens,
                inflight=inflight,
                translation_target=tgt,
            )
        ]

    prompt = _prompt_for_segments(segments, translation_target=tgt)
    settings = get_pipeline_settings()
    max_retries = settings.gpt_max_retries
    last_err: Exception | None = None
    batch_rf_formats: list[dict[str, object] | None] = []
    if settings.translate_batch_response_json:
        if (
            settings.translate_batch_use_structured_json_schema
            and model_supports_structured_outputs_json_schema(model)
            and len(segments)
            >= settings.translate_batch_structured_schema_min_segments
        ):
            batch_rf_formats.append(
                batch_segments_json_schema_response_format(len(segments))
            )
        if model_supports_response_format_json_object(model):
            batch_rf_formats.append({"type": "json_object"})
    batch_rf_formats.append(None)
    rf_idx = 0
    api_failures = 0
    while True:
        if api_failures >= max_retries:
            detail = (
                openai_user_facing_message(last_err)
                if last_err
                else "unknown error"
            )
            raise RuntimeError(
                f"OpenAI batch failed after {max_retries} attempts: {detail}"
            )

        kwargs: dict = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            **temperature_kw(model, float(settings.translation_temperature)),
            **completion_token_params(model, _completion_budget_multi(segments)),
        }
        rf = batch_rf_formats[min(rf_idx, len(batch_rf_formats) - 1)]
        if rf is not None:
            kwargs["response_format"] = rf

        try:
            async with inflight:
                resp = await client.chat.completions.create(**kwargs)
        except (RateLimitError, APITimeoutError) as e:
            last_err = e
            api_failures += 1
            wait = _openai_retry_sleep_seconds(e, api_failures - 1)
            logger.warning(
                "OpenAI batch transient (attempt %s/%s): %s; sleeping %.1fs",
                api_failures,
                max_retries,
                e,
                wait,
            )
            await asyncio.sleep(wait)
            continue
        except APIError as e:
            last_err = e
            sc = getattr(e, "status_code", None)
            if sc == 429 or _openai_http_status_is_transient_server_error(sc):
                api_failures += 1
                wait = _openai_retry_sleep_seconds(e, api_failures - 1)
                kind = "429" if sc == 429 else f"HTTP {sc}"
                logger.warning(
                    "OpenAI batch transient (%s, attempt %s/%s): %s; sleeping %.1fs",
                    kind,
                    api_failures,
                    max_retries,
                    e,
                    wait,
                )
                await asyncio.sleep(wait)
                continue
            if sc == 400 and rf_idx + 1 < len(batch_rf_formats):
                rf_idx += 1
                logger.warning(
                    "OpenAI batch HTTP 400; retrying with looser response_format (step %s/%s): %s",
                    rf_idx,
                    len(batch_rf_formats) - 1,
                    e,
                )
                continue
            if sc == 400:
                logger.warning("OpenAI batch HTTP 400: %s", e)
            raise RuntimeError(openai_user_facing_message(e)) from e

        choice = resp.choices[0]
        msg = choice.message
        content = (msg.content or "").strip()
        if not content and getattr(msg, "refusal", None):
            content = (msg.refusal or "").strip()
        finish = getattr(choice, "finish_reason", None)
        usage = getattr(resp, "usage", None)
        if usage is not None and on_tokens is not None:
            total = int(getattr(usage, "total_tokens", None) or 0)
            if total:
                on_tokens(total)

        parsed_set = _parse_multi_segment_json(content, len(segments))
        if parsed_set is not None:
            return [sanitize_translated_output(s) for s in parsed_set]

        if rf_idx + 1 < len(batch_rf_formats):
            rf_idx += 1
            logger.warning(
                "Batch JSON parse failed (n=%s finish_reason=%s); retrying multi-segment "
                "with looser response_format (format_step=%s/%s)",
                len(segments),
                finish,
                rf_idx,
                len(batch_rf_formats) - 1,
            )
            continue

        break

    fb_parallel = _fallback_single_segment_parallelism()
    logger.warning(
        "Batch JSON parse failed after all response_format tiers (n=%s); "
        "falling back to single completions (max_parallel=%s)",
        len(segments),
        fb_parallel,
    )
    return await _translate_segments_parallel_limited(
        client,
        model,
        segments,
        on_tokens=on_tokens,
        max_parallel=fb_parallel,
        inflight=inflight,
        translation_target=tgt,
    )


async def _async_translate_multi(
    client: AsyncOpenAI,
    model: str,
    segments: list[str],
    *,
    on_tokens: Callable[[int], None] | None = None,
    inflight: asyncio.Semaphore,
    translation_target: str,
) -> list[str]:
    tgt = normalize_translation_target(translation_target)
    if len(segments) == 1:
        return [
            await _async_translate_one(
                client,
                model,
                segments[0],
                on_tokens=on_tokens,
                inflight=inflight,
                translation_target=tgt,
            )
        ]

    segments = [sanitize_user_text(s) for s in segments]
    settings = get_pipeline_settings()
    temp = finite_temperature(float(settings.translation_temperature))
    merged: list[str | None] = [None] * len(segments)
    miss_idx: list[int] = []

    if settings.translation_cache_enabled and settings.redis_url:

        def _lookup() -> list[str | None]:
            from app.services.translation_cache import lookup_cached_translations

            return lookup_cached_translations(
                segments, model=model, temperature=temp, translation_target=tgt
            )

        cached_row = await asyncio.to_thread(_lookup)
    else:
        cached_row = [None] * len(segments)

    for i, seg in enumerate(segments):
        if not seg.strip():
            merged[i] = seg
        elif cached_row[i] is not None:
            merged[i] = cached_row[i]
        else:
            miss_idx.append(i)

    if not miss_idx:
        return [sanitize_translated_output(str(merged[i])) for i in range(len(segments))]

    work_segs = [segments[i] for i in miss_idx]
    out_work = await _openai_translate_multi_segment(
        client,
        model,
        work_segs,
        on_tokens=on_tokens,
        inflight=inflight,
        translation_target=tgt,
    )
    if len(out_work) != len(miss_idx):
        raise RuntimeError(
            f"Batch output size mismatch: got {len(out_work)}, expected {len(miss_idx)}"
        )
    for j, i in enumerate(miss_idx):
        merged[i] = out_work[j]

    if settings.translation_cache_enabled and settings.redis_url:

        def _store() -> None:
            from app.services.translation_cache import store_cached_translations

            store_cached_translations(
                work_segs,
                out_work,
                model=model,
                temperature=temp,
                translation_target=tgt,
            )

        await asyncio.to_thread(_store)

    return [sanitize_translated_output(str(merged[i])) for i in range(len(segments))]


def pack_segment_indices(
    segment_texts: list[str],
    *,
    max_batch_input_tokens: int,
    max_segments_per_batch: int | None = None,
) -> list[list[int]]:
    """Greedy pack segment indices into batches bounded by tokens and optional segment count."""
    batches: list[list[int]] = []
    cur: list[int] = []
    cur_tok = 0
    overhead_per_seg = 80
    cap = int(max_segments_per_batch) if max_segments_per_batch else 0

    for i, text in enumerate(segment_texts):
        tok = count_tokens(text) + overhead_per_seg
        if cur:
            token_full = cur_tok + tok > max_batch_input_tokens
            seg_full = cap > 0 and len(cur) >= cap
            if token_full or seg_full:
                batches.append(cur)
                cur = []
                cur_tok = 0
        cur.append(i)
        cur_tok += tok
    if cur:
        batches.append(cur)
    return batches


def build_async_openai_client() -> AsyncOpenAI:
    """Create an AsyncOpenAI with a pooled httpx backend (suitable for one long-lived chunk_worker)."""
    settings = get_pipeline_settings()
    lim = max(
        64,
        int(settings.openai_http_max_connections),
        settings.translate_batch_max_concurrency * 4,
        settings.chunk_worker_parallel_handlers * 8,
    )
    http_client = httpx.AsyncClient(
        limits=httpx.Limits(
            max_connections=lim,
            max_keepalive_connections=max(32, lim // 2),
        ),
        timeout=httpx.Timeout(300.0, connect=60.0),
    )
    return AsyncOpenAI(
        api_key=settings.openai_api_key,
        timeout=httpx.Timeout(300.0, connect=60.0),
        max_retries=0,
        http_client=http_client,
    )


def _async_client() -> AsyncOpenAI:
    return build_async_openai_client()


async def translate_segments_batched_async(
    segment_texts: list[str],
    *,
    on_tokens: Callable[[int], None] | None = None,
    on_batch_done: Callable[[int, int], None] | None = None,
    on_batch_timing: Callable[[int, int, float], None] | None = None,
    on_translation_pulse: Callable[[float], None] | None = None,
    translation_target: str = "hinglish",
) -> list[str]:
    """Translate all segments; results align 1:1 with ``segment_texts``."""
    settings = get_pipeline_settings()
    if not settings.openai_api_key:
        raise RuntimeError("OPENAI_API_KEY is not configured")
    if not segment_texts:
        return []

    model = normalize_openai_model(settings.openai_model)
    max_batch = settings.translate_api_batch_max_input_tokens
    reserve = settings.translate_api_batch_prompt_reserve_tokens
    budget = max(1500, int(max_batch) - int(reserve))
    concurrency = max(1, settings.translate_batch_max_concurrency)
    stagger_ms = max(0.0, float(settings.translate_batch_stagger_ms))
    inflight_cap = max(1, int(settings.translate_openai_max_inflight))
    if inflight_cap < concurrency:
        logger.warning(
            "translate_openai_max_inflight (%s) is below translate_batch_max_concurrency (%s); "
            "OpenAI calls are capped at %s parallel requests. Set TRANSLATE_OPENAI_MAX_INFLIGHT "
            "to at least %s for full throughput (or lower TRANSLATE_BATCH_MAX_CONCURRENCY).",
            inflight_cap,
            concurrency,
            inflight_cap,
            concurrency,
        )

    seg_cap = max(8, int(settings.translate_api_batch_max_segments))
    batches = pack_segment_indices(
        segment_texts,
        max_batch_input_tokens=budget,
        max_segments_per_batch=seg_cap,
    )
    logger.info(
        "translate_plan batches=%s segments=%s budget_tokens=%s max_segments_per_batch=%s "
        "concurrency=%s stagger_ms=%s openai_max_inflight=%s",
        len(batches),
        len(segment_texts),
        budget,
        seg_cap,
        concurrency,
        stagger_ms,
        settings.translate_openai_max_inflight,
    )
    results: list[str | None] = [None] * len(segment_texts)
    lock = asyncio.Lock()
    done_batches = 0

    client = _async_client()
    sem = asyncio.Semaphore(concurrency)
    inflight = asyncio.Semaphore(max(1, settings.translate_openai_max_inflight))

    async def run_batch(batch_idx: int, indices: list[int]) -> None:
        nonlocal done_batches
        # Small jobs: all batches fit in one concurrency wave — skip artificial delay.
        if stagger_ms > 0 and batch_idx > 0 and len(batches) > concurrency:
            # Cap so very large documents do not add minutes of idle delay.
            delay_s = min(45.0, (stagger_ms / 1000.0) * batch_idx)
            delay_s += random.random() * 0.03
            await asyncio.sleep(delay_s)
        segs = [segment_texts[j] for j in indices]
        async with sem:
            t0 = time.perf_counter()
            out = await _async_translate_multi(
                client,
                model,
                segs,
                on_tokens=on_tokens,
                inflight=inflight,
                translation_target=translation_target,
            )
            dt = time.perf_counter() - t0
            logger.info(
                "translate_batch_done batch=%s segments=%s wall_s=%.3f",
                batch_idx,
                len(indices),
                dt,
            )
            if on_batch_timing is not None:
                on_batch_timing(batch_idx, len(indices), dt)
        if len(out) != len(indices):
            raise RuntimeError(
                f"Batch output size mismatch: got {len(out)}, expected {len(indices)}"
            )
        for idx, text in zip(indices, out, strict=True):
            results[idx] = text
        if on_batch_done is not None:
            async with lock:
                done_batches += 1
                on_batch_done(done_batches, len(batches))

    pulse_interval = float(
        getattr(settings, "translation_progress_pulse_interval_s", 0.0) or 0.0
    )
    translate_t0 = time.perf_counter()
    pulse_task: asyncio.Task[None] | None = None
    if on_translation_pulse is not None and pulse_interval > 0:

        async def _pulse_loop() -> None:
            try:
                while True:
                    await asyncio.sleep(pulse_interval)
                    # Same lock as on_batch_done: pulse used to read stale prog_state and
                    # publish a lower % after a batch callback (bar jumping backward).
                    async with lock:
                        on_translation_pulse(time.perf_counter() - translate_t0)
            except asyncio.CancelledError:
                return

        pulse_task = asyncio.create_task(_pulse_loop())
    try:
        try:
            await asyncio.gather(
                *[run_batch(bi, idxs) for bi, idxs in enumerate(batches)]
            )
        finally:
            if pulse_task is not None:
                pulse_task.cancel()
                try:
                    await pulse_task
                except asyncio.CancelledError:
                    pass

        if any(x is None for x in results):
            raise RuntimeError("Incomplete batched translation")
        return [results[i] for i in range(len(results))]
    finally:
        await client.close()


def translate_segments_batched_sync(
    segment_texts: list[str],
    *,
    on_tokens: Callable[[int], None] | None = None,
    on_batch_done: Callable[[int, int], None] | None = None,
    on_batch_timing: Callable[[int, int, float], None] | None = None,
    on_translation_pulse: Callable[[float], None] | None = None,
    translation_target: str = "hinglish",
) -> list[str]:
    return asyncio.run(
        translate_segments_batched_async(
            segment_texts,
            on_tokens=on_tokens,
            on_batch_done=on_batch_done,
            on_batch_timing=on_batch_timing,
            on_translation_pulse=on_translation_pulse,
            translation_target=translation_target,
        )
    )


async def translate_one_chunk_batch_async(
    segments: list[str],
    *,
    on_tokens: Callable[[int], None] | None = None,
    openai_client: AsyncOpenAI | None = None,
    on_translation_pulse: Callable[[float], None] | None = None,
    translation_target: str = "hinglish",
) -> tuple[list[str], float]:
    """One chunk-queue job: translate ``segments`` (subset of document) with minimal round-trips.

    Pass ``openai_client`` from a long-lived chunk_worker process to reuse HTTP connections.
    """
    if not segments:
        return [], 0.0
    settings = get_pipeline_settings()
    if not settings.openai_api_key:
        raise RuntimeError("OPENAI_API_KEY is not configured")
    own_client = openai_client is None
    client = openai_client or _async_client()
    model = normalize_openai_model(settings.openai_model)
    inflight = asyncio.Semaphore(max(1, settings.translate_openai_max_inflight))
    t0 = time.perf_counter()
    pulse_interval = float(
        getattr(settings, "translation_progress_pulse_interval_s", 0.0) or 0.0
    )
    translate_t0 = time.perf_counter()
    pulse_task: asyncio.Task[None] | None = None
    if on_translation_pulse is not None and pulse_interval > 0:

        async def _pulse_loop() -> None:
            try:
                while True:
                    await asyncio.sleep(pulse_interval)
                    on_translation_pulse(time.perf_counter() - translate_t0)
            except asyncio.CancelledError:
                return

        pulse_task = asyncio.create_task(_pulse_loop())
    try:
        out = await _async_translate_multi(
            client,
            model,
            segments,
            on_tokens=on_tokens,
            inflight=inflight,
            translation_target=translation_target,
        )
        return out, time.perf_counter() - t0
    finally:
        if pulse_task is not None:
            pulse_task.cancel()
            try:
                await pulse_task
            except asyncio.CancelledError:
                pass
        if own_client:
            await client.close()


def translate_one_chunk_batch_sync(
    segments: list[str],
    *,
    on_tokens: Callable[[int], None] | None = None,
    translation_target: str = "hinglish",
) -> tuple[list[str], float]:
    return asyncio.run(
        translate_one_chunk_batch_async(
            segments, on_tokens=on_tokens, translation_target=translation_target
        )
    )
