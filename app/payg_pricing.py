"""Pay-as-you-go INR formula — single place to change backend PAYG math."""

from __future__ import annotations

# Final INR charged per 100,000 billable words (single blended rate; rounded per estimate).
PAYG_INR_PER_100K_WORDS: float = 99.0


def estimate_payg_inr(words: int) -> float:
    """Proportional rate, rounded to whole rupees (upload estimate / settlement)."""
    w = max(0, int(words))
    return float(round((w / 100_000) * PAYG_INR_PER_100K_WORDS))
