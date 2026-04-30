"""Per-theme CSS (kept in sync with ``frontend/.../styles/*.css``)."""

from __future__ import annotations

from pathlib import Path

from app.services.document_template_render.models import DocumentTemplateType

_TEMPLATE_CSS: dict[DocumentTemplateType, str] | None = None
_INDENTED_CSS: dict[DocumentTemplateType, str] | None = None


def _styles_dir() -> Path:
    return Path(__file__).resolve().parent / "styles"


def _load_styles() -> dict[DocumentTemplateType, str]:
    global _TEMPLATE_CSS
    if _TEMPLATE_CSS is not None:
        return _TEMPLATE_CSS
    out: dict[DocumentTemplateType, str] = {}
    d = _styles_dir()
    for name in (
        "ebook",
        "report",
        "minimal",
        "blog",
        "academic",
        "bilingual",
    ):
        out[name] = (d / f"{name}.css").read_text(encoding="utf-8")
    _TEMPLATE_CSS = out
    return out


def ensure_styles_and_indents_cached() -> None:
    """Load raw + indented theme CSS once (used by ``init_document_template_render``)."""
    _indented_map()


def _indented_map() -> dict[DocumentTemplateType, str]:
    global _INDENTED_CSS
    if _INDENTED_CSS is None:
        styles = _load_styles()
        _INDENTED_CSS = {
            k: indent_css_for_style_tag(v) for k, v in styles.items()
        }
    return _INDENTED_CSS


def css_for_template(template_id: DocumentTemplateType) -> str:
    return _load_styles()[template_id]


def indented_css_for_template(template_id: DocumentTemplateType) -> str:
    """Indented theme CSS for embedding in Jinja (computed once per process)."""
    return _indented_map()[template_id]


def indent_css_for_style_tag(css: str) -> str:
    """Match frontend ``indentCssForStyleTag`` for readable HTML source."""
    return "\n".join(
        f"      {line}" if line else line for line in css.split("\n")
    )
