"""Turn OpenAI client exceptions into short, user-safe strings (no HTML dumps)."""

from __future__ import annotations

import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

_HTMLISH = re.compile(r"(?is)<\s*!DOCTYPE\s+html\s*>|<\s*html\b")


def _looks_like_html(text: str) -> bool:
    s = text[:8000]
    if _HTMLISH.search(s):
        return True
    low = s.lower()
    return "<html" in low and ("<head" in low or "<body" in low)


def openai_user_facing_message(exc: BaseException, *, max_len: int = 420) -> str:
    """Single line for RuntimeError / stored job error_message. Logs may still use str(exc)."""
    raw = (str(exc) or "").strip() or type(exc).__name__
    sc: Any = getattr(exc, "status_code", None)
    try:
        code = int(sc) if sc is not None else None
    except (TypeError, ValueError):
        code = None

    if _looks_like_html(raw):
        logger.error(
            "OpenAI client error body looks like HTML (proxy/firewall/wrong host). status=%s",
            code,
        )
        hint = (
            "The service got an HTML page instead of the OpenAI JSON API—usually a proxy, "
            "corporate firewall, VPN, DNS filter, or an incorrect OPENAI_BASE_URL. "
            "Verify OPENAI_API_KEY, optional proxy settings, and that https://api.openai.com is reachable."
        )
        if code is not None:
            return f"OpenAI API error (HTTP {code}): {hint}"
        return f"OpenAI API error: {hint}"

    if len(raw) > max_len:
        raw = raw[: max_len - 3] + "..."
    if code is not None:
        return f"OpenAI API error (HTTP {code}): {raw}"
    return f"OpenAI API error: {raw}"
