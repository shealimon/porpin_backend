"""Redis-backed cache for identical source segments (reduces duplicate OpenAI calls)."""

from __future__ import annotations

import hashlib
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from redis import Redis

logger = logging.getLogger(__name__)

_CACHE_KEY_PREFIX = "translator:tx:v2"


def _redis_available() -> bool:
    from app.core.pipeline_settings import get_pipeline_settings

    s = get_pipeline_settings()
    return bool(s.redis_url and s.translation_cache_enabled)


def _cache_key(
    model: str, temperature: float, text: str, translation_target: str
) -> str:
    h = hashlib.sha256(
        f"{model}\x1f{temperature:.6f}\x1f{translation_target}\x1f{text}".encode(
            "utf-8", errors="surrogatepass"
        )
    ).hexdigest()
    return f"{_CACHE_KEY_PREFIX}:{h}"


def _conn() -> Redis:
    from app.jobs.redis_sync import get_sync_redis

    return get_sync_redis()


def lookup_cached_translations(
    segments: list[str],
    *,
    model: str,
    temperature: float,
    translation_target: str = "hinglish",
) -> list[str | None]:
    """
    Return a list parallel to ``segments`` with cached Hinglish or None on miss.

    Empty / whitespace-only segments are returned as None here; callers substitute the source.
    """
    if not segments or not _redis_available():
        return [None] * len(segments)

    indices: list[int] = []
    keys: list[str] = []
    for i, seg in enumerate(segments):
        if not (seg and seg.strip()):
            continue
        indices.append(i)
        keys.append(_cache_key(model, temperature, seg, translation_target))

    if not keys:
        return [None] * len(segments)

    try:
        r = _conn()
        vals = r.mget(keys) or []
    except Exception:
        logger.warning("translation cache mget failed", exc_info=True)
        return [None] * len(segments)

    if len(vals) != len(keys):
        logger.warning("translation cache mget length mismatch")
        return [None] * len(segments)

    out: list[str | None] = [None] * len(segments)
    for idx, raw in zip(indices, vals, strict=True):
        if raw is not None and isinstance(raw, str) and raw:
            out[idx] = raw
    return out


def store_cached_translations(
    segments: list[str],
    translations: list[str],
    *,
    model: str,
    temperature: float,
    translation_target: str = "hinglish",
) -> None:
    if not segments or not _redis_available():
        return
    if len(translations) != len(segments):
        return

    from app.core.pipeline_settings import get_pipeline_settings

    ttl = int(get_pipeline_settings().translation_cache_ttl_seconds)

    try:
        r = _conn()
        pipe = r.pipeline()
        n = 0
        for seg, tx in zip(segments, translations, strict=True):
            if not (seg and seg.strip()):
                continue
            if not (tx and str(tx).strip()):
                continue
            if tx == seg:
                continue
            pipe.set(
                _cache_key(model, temperature, seg, translation_target), tx, ex=ttl
            )
            n += 1
        if n:
            pipe.execute()
    except Exception:
        logger.warning("translation cache store failed", exc_info=True)
