"""Normalize Chat Completions payloads: sanitization + model-specific token/temperature rules."""

from __future__ import annotations

import math
import re
import unicodedata

# Align with batch_translator caps for chat.completions output.
_MAX_CHAT_OUTPUT_TOKENS = 16_384
MAX_CHAT_OUTPUT_TOKENS_CAP = _MAX_CHAT_OUTPUT_TOKENS


def normalize_openai_model(model: str | None) -> str:
    """Strip whitespace / stray newlines from env-sourced model ids."""
    return (model or "").strip()


def sanitize_user_text(text: str) -> str:
    """Strip NULs, BOM, lone surrogates, and most control chars (PDF/Word junk can break request JSON)."""
    if not isinstance(text, str):
        text = str(text)
    if not text:
        return text
    text = text.replace("\x00", "")
    if text.startswith("\ufeff"):
        text = text[1:]
    # Lone UTF-16 surrogates are invalid in UTF-8 on some stacks; replace.
    text = "".join(
        ch if not (0xD800 <= ord(ch) <= 0xDFFF) else "\ufffd" for ch in text
    )
    out: list[str] = []
    for ch in text:
        o = ord(ch)
        if o < 32 and ch not in "\t\n\r":
            out.append(" ")
        elif 0x7F <= o < 0xA0:
            out.append(" ")
        else:
            out.append(ch)
    text = "".join(out)
    try:
        text = unicodedata.normalize("NFC", text)
    except Exception:
        pass
    return text


# --- Model-output cleanup (bad batch completions / echoed instructions) ---
_LINE_ONLY_JSON_ECHO = re.compile(
    r"(?is)^(?:\s*(?:assistant\s+)?(?:to\s*=\s*json\s+code\b\s*)){3,}$"
)

# Typical PDF recto/verso bleed: "WHITE NIGHTS WHITE NIGHTS 14"
_LINE_DUP_ALLCAPS_RUNNING_HEAD = re.compile(
    r"(?m)^(?P<t>[A-Z]{2,}(?:\s+[A-Z]{2,}){0,8})\s+(?P=t)(?:\s+\d{1,4})?\s*$"
)

_INLINE_JSON_ECHO_RUN = re.compile(
    r"(?is)(?:\bassistant\b\s*)?(?:to\s*=\s*json\s+code\b\s*){2,}"
)


def sanitize_translated_output(text: str | None) -> str:
    """Strip instruction echo / JSON-batch junk models sometimes leak into segment strings."""
    if text is None:
        return ""
    if not isinstance(text, str):
        text = str(text)
    raw = sanitize_user_text(text)
    if not raw.strip():
        return raw.strip()
    t = raw.replace("\r\n", "\n").replace("\r", "\n")
    out_lines: list[str] = []
    for line in t.split("\n"):
        stripped = line.strip()
        if not stripped:
            out_lines.append("")
            continue
        if _LINE_ONLY_JSON_ECHO.fullmatch(stripped):
            continue
        m_head = _LINE_DUP_ALLCAPS_RUNNING_HEAD.fullmatch(stripped)
        if m_head is not None and len(m_head.group("t").replace(" ", "")) >= 6:
            continue
        cleaned = _INLINE_JSON_ECHO_RUN.sub(" ", line)
        cleaned = " ".join(cleaned.split())
        if cleaned.strip():
            out_lines.append(cleaned.strip())
        else:
            out_lines.append("")
    result = "\n".join(out_lines)
    while "\n\n\n" in result:
        result = result.replace("\n\n\n", "\n\n")
    return result.strip()


def finite_temperature(temp: float, *, default: float = 0.0) -> float:
    try:
        v = float(temp)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(v):
        return default
    return max(0.0, min(2.0, v))


def completion_token_params(model: str, max_out: int) -> dict[str, int]:
    """Use ``max_completion_tokens`` for o-series / GPT-5; ``max_tokens`` for gpt-4* and older chat models."""
    m = normalize_openai_model(model).lower()
    try:
        mo = int(max_out)
    except (TypeError, ValueError):
        mo = _MAX_CHAT_OUTPUT_TOKENS
    if not math.isfinite(float(mo)):
        mo = _MAX_CHAT_OUTPUT_TOKENS
    mt = max(1, min(int(mo), _MAX_CHAT_OUTPUT_TOKENS))
    if m.startswith(("o1", "o3", "o4")) or "gpt-5" in m:
        return {"max_completion_tokens": mt}
    return {"max_tokens": mt}


def temperature_kw(model: str, temp: float) -> dict[str, float]:
    """o-series chat models reject ``temperature``; omit it for those."""
    m = normalize_openai_model(model).lower()
    if m.startswith(("o1", "o3", "o4")):
        return {}
    return {"temperature": finite_temperature(temp)}


def model_supports_response_format_json_object(model: str) -> bool:
    m = normalize_openai_model(model).lower()
    if m.startswith(("o1", "o3", "o4")):
        return False
    return True


def model_supports_structured_outputs_json_schema(model: str) -> bool:
    """Structured Outputs (json_schema); same rough compatibility as json_object mode."""
    return model_supports_response_format_json_object(model)


def batch_segments_json_schema_response_format(num_segments: int) -> dict[str, object]:
    """OpenAI Chat Completions ``response_format`` with strict per-index string fields."""
    n = max(1, int(num_segments))
    keys = [str(i) for i in range(n)]
    schema: dict[str, object] = {
        "type": "object",
        "properties": {k: {"type": "string"} for k in keys},
        "required": keys,
        "additionalProperties": False,
    }
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "batch_hinglish_segments",
            "strict": True,
            "schema": schema,
        },
    }
