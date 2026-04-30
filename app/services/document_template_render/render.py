from __future__ import annotations

from pathlib import Path
from typing import cast

from jinja2 import DictLoader, Environment, select_autoescape

from app.services.document_template_render.context import build_template_context
from app.services.document_template_render.css import (
    ensure_styles_and_indents_cached,
    indented_css_for_template,
)
from app.services.document_template_render.models import (
    DEFAULT_DOCUMENT_TEMPLATE,
    DocumentForTemplate,
    DocumentTemplateType,
    is_document_template_type,
)

_TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
_jinja_env: Environment | None = None


def _build_jinja_env_from_memory(sources: dict[str, str]) -> Environment:
    return Environment(
        loader=DictLoader(sources),
        autoescape=select_autoescape(["html", "j2", "jinja"]),
        trim_blocks=True,
        lstrip_blocks=True,
        auto_reload=False,
        cache_size=400,
    )


def init_document_template_render() -> None:
    """Load all Jinja sources and theme CSS from disk once; warm template cache.

    Safe to call multiple times. Call from the API lifespan to avoid per-request I/O
    and first-hit compile latency.
    """
    global _jinja_env
    if _jinja_env is not None:
        return
    tdir = _TEMPLATES_DIR
    if not tdir.is_dir():
        raise RuntimeError("document_template_render: templates directory missing")
    sources = {p.name: p.read_text(encoding="utf-8") for p in tdir.glob("*.j2")}
    if not sources:
        raise RuntimeError("document_template_render: no .j2 templates in templates/")
    _jinja_env = _build_jinja_env_from_memory(sources)
    ensure_styles_and_indents_cached()
    # Prime compiled template; subsequent renders reuse the cached Template.
    _jinja_env.get_template("layout.j2")


def _get_jinja_env() -> Environment:
    if _jinja_env is None:
        init_document_template_render()
    assert _jinja_env is not None
    return _jinja_env


def resolve_template_type(
    raw: str | None,
) -> DocumentTemplateType:
    """Unknown or missing values map to the default (``report``)."""
    if raw is not None and is_document_template_type(raw):
        return cast(DocumentTemplateType, raw)
    return DEFAULT_DOCUMENT_TEMPLATE


def render_document_html(
    model: DocumentForTemplate,
    template_id: str | None = None,
) -> str:
    """Full HTML document: theme CSS in ``<style>``, body classes match the layout template."""
    resolved = resolve_template_type(template_id)
    css = indented_css_for_template(resolved)
    context = build_template_context(model, resolved, css)
    return _get_jinja_env().get_template("layout.j2").render(**context)
