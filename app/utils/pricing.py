"""INR cost estimates from token counts (aligned with product minimum + per-10k words)."""

from __future__ import annotations

import math

from app.core.pipeline_settings import get_pipeline_settings


def estimate_cost_inr_from_tokens(tokens_used: int) -> float:
    """Rough cost: treat tokens as proportional to billable words."""
    s = get_pipeline_settings()
    minimum = s.minimum_charge_inr
    rate = s.rate_inr_per_10000_words
    words = max(0, tokens_used) * 0.75
    raw = (words / 10_000) * rate
    due = max(minimum, raw)
    return math.ceil(due * 100) / 100
