"""Shared synchronous Redis client with a connection pool (RQ paths, chunk counters, quota)."""

from __future__ import annotations

import logging

from redis import Redis

from app.core.pipeline_settings import get_pipeline_settings

logger = logging.getLogger(__name__)

_client: Redis | None = None


def get_sync_redis() -> Redis:
    """Process-wide pooled Redis for high concurrency (avoid per-call TCP connect overhead)."""
    global _client
    if _client is None:
        settings = get_pipeline_settings()
        url = settings.redis_url
        if not url:
            raise RuntimeError("REDIS_URL is not configured")
        _client = Redis.from_url(
            url,
            decode_responses=True,
            max_connections=settings.redis_max_connections,
        )
    return _client
