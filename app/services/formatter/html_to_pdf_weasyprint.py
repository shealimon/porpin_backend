"""Render HTML (from document templates or any full document) to PDF via WeasyPrint.

CSS embedded in ``<style>`` tags is applied. This module injects a small print stylesheet
for page size/margins, footer page numbers, section breaks, and flat-layout heading breaks.

**Runtime dependencies:** WeasyPrint needs Pango, Cairo, and GObject on the host. On Linux
this is usually ``apt install libpango-1.0-0 libpangocairo-1.0-0 …``; on Windows install
the GTK3 runtime linked from WeasyPrint’s installation guide. The Docker image installs
the required Debian packages.

Relative ``url(...)`` in CSS (e.g. ``@font-face``) resolve against ``base_url`` (defaults
to the ``app`` package directory so ``assets/fonts/...`` matches ``book_typography`` paths).
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from app.services.formatter.book_typography import font_files_present

logger = logging.getLogger(__name__)

_APP_DIR = Path(__file__).resolve().parents[2]


def default_html_base_url() -> str:
    """Directory URI for resolving relative assets (fonts, optional linked CSS)."""
    u = _APP_DIR.as_uri()
    return u if u.endswith("/") else f"{u}/"


_LIBRE_BASKERVILLE_FACES_CSS: str | None = None
_PRINT_LAYOUT_CSS_STATIC: str | None = None
_WEASYPRINT_HTML: type | None = None


def _libre_baskerville_font_faces_css() -> str:
    global _LIBRE_BASKERVILLE_FACES_CSS
    if _LIBRE_BASKERVILLE_FACES_CSS is not None:
        return _LIBRE_BASKERVILLE_FACES_CSS
    if not font_files_present():
        _LIBRE_BASKERVILLE_FACES_CSS = ""
        return _LIBRE_BASKERVILLE_FACES_CSS
    _LIBRE_BASKERVILLE_FACES_CSS = """
@font-face {
  font-family: "Libre Baskerville";
  src: url("assets/fonts/LibreBaskerville-Regular.ttf") format("truetype");
  font-weight: 400;
  font-style: normal;
}
@font-face {
  font-family: "Libre Baskerville";
  src: url("assets/fonts/LibreBaskerville-Bold.ttf") format("truetype");
  font-weight: 700;
  font-style: normal;
}
"""
    return _LIBRE_BASKERVILLE_FACES_CSS


def _print_layout_css() -> str:
    global _PRINT_LAYOUT_CSS_STATIC
    if _PRINT_LAYOUT_CSS_STATIC is not None:
        return _PRINT_LAYOUT_CSS_STATIC
    _PRINT_LAYOUT_CSS_STATIC = """
@page {
  size: A4;
  margin: 20mm 18mm 26mm 18mm;
  @top-center {
    content: string(doc_title);
    font-size: 9pt;
    color: #64748b;
    font-family: "Libre Baskerville", "Segoe UI", "DejaVu Sans", sans-serif;
  }
  @top-right {
    content: string(chapter_title);
    font-size: 9pt;
    color: #94a3b8;
    font-family: "Libre Baskerville", "Segoe UI", "DejaVu Sans", sans-serif;
  }
  @bottom-center {
    content: counter(page);
    font-size: 9pt;
    color: #64748b;
    font-family: "Libre Baskerville", "Segoe UI", "DejaVu Sans", sans-serif;
  }
}

@page :first {
  @top-center { content: none; }
  @top-right { content: none; }
}

@page chapter {
  @top-center { content: none; }
  @top-right { content: none; }
  @bottom-center { content: none; }
}

/* Reader-first: comfortable type, spacing, and contrast for long reading on paper/PDF. */
.doc-root {
  min-height: auto !important;
  background: #ffffff !important;
  -webkit-font-smoothing: auto;
  font-size: 11.5pt !important;
  line-height: 1.62 !important;
  color: #171717 !important;
}

.doc-title:not(.doc-title--default) {
  string-set: doc_title content(text);
}

.doc-landmark .doc-title {
  font-size: 1.65rem !important;
  line-height: 1.28 !important;
  letter-spacing: -0.02em !important;
  color: #0f172a !important;
}

.doc-article {
  max-width: none !important;
  margin: 0 !important;
  padding: 0 !important;
  border: none !important;
  box-shadow: none !important;
  border-radius: 0 !important;
  min-height: auto !important;
}

/*
 * Chapter page breaks on a wrapper block (not the h* itself) so WeasyPrint does not
 * fight `break-after: avoid` on `.doc-heading`. `.doc-chapter-page` comes from Jinja.
 */
section.doc-chapter,
.doc-chapter-page {
  break-before: page;
  page-break-before: always;
  /* Keep chapter title with the start of the following body text when possible. */
  break-after: avoid;
  page-break-after: avoid;
}

.doc-chapter-opener {
  page: chapter;
  display: block;
  text-align: center;
  padding-top: 32mm;
  padding-bottom: 10mm;
}

.doc-chapter-number {
  font-family: "Libre Baskerville", "Georgia", "Times New Roman", serif;
  font-size: 56pt;
  font-weight: 700;
  line-height: 1;
  letter-spacing: -0.03em;
  color: #0f172a;
  margin: 0 0 6mm;
}

.doc-chapter-ornament {
  display: block;
  width: 34mm;
  height: 2px;
  margin: 0 auto 8mm;
  background: #94a3b8;
  opacity: 0.65;
  border-radius: 999px;
}

.doc-chapter-title {
  string-set: chapter_title content(text);
  font-size: 52pt !important;
  line-height: 1.08 !important;
  letter-spacing: -0.02em !important;
  color: #0f172a !important;
  font-weight: 700 !important;
  margin: 0 auto 0 !important;
  max-width: 140mm;
}

.doc-chapter-start,
section.doc-chapter > h2.doc-heading--2:first-of-type {
  text-align: center !important;
  font-size: 18pt !important;
  font-weight: 700 !important;
  margin-top: 0 !important;
  margin-bottom: 1em !important;
  margin-left: auto !important;
  margin-right: auto !important;
}

.doc-heading {
  break-after: avoid;
  page-break-after: avoid;
}

.doc-heading:not(.doc-chapter-start):not(.doc-title) {
  color: #111827 !important;
}

.doc-heading--3 {
  font-size: 13pt !important;
  font-style: italic !important;
  font-weight: 600 !important;
  color: #334155 !important;
  margin-top: 1.4rem !important;
  margin-bottom: 0.6rem !important;
}

.doc-heading--4 {
  font-size: 12pt !important;
  font-weight: 700 !important;
  color: #0f172a !important;
  margin-top: 1.15rem !important;
  margin-bottom: 0.5rem !important;
}

.doc-heading--5 {
  font-size: 11.25pt !important;
  font-weight: 700 !important;
  color: #0f172a !important;
  margin-top: 1.0rem !important;
  margin-bottom: 0.45rem !important;
}

.doc-heading--6 {
  font-size: 11pt !important;
  font-weight: 600 !important;
  color: #475569 !important;
  margin-top: 0.9rem !important;
  margin-bottom: 0.4rem !important;
}

.doc-paragraph {
  font-size: 11.5pt !important;
  line-height: 1.68 !important;
  color: #171717 !important;
  text-align: justify !important;
  hyphens: auto;
  orphans: 3;
  widows: 3;
  margin-top: 0 !important;
  margin-bottom: 0.85em !important;
}

/* Book-like paragraph rhythm: indent consecutive paragraphs, not the first after a heading. */
.doc-paragraph + .doc-paragraph {
  text-indent: 1.25em;
  margin-top: 0 !important;
}
h2 + .doc-paragraph,
h3 + .doc-paragraph,
h4 + .doc-paragraph,
h5 + .doc-paragraph,
h6 + .doc-paragraph {
  text-indent: 0;
}

.doc-list {
  font-size: 11.5pt !important;
  line-height: 1.62 !important;
  color: #171717 !important;
  margin-top: 0.35em !important;
  margin-bottom: 1em !important;
  orphans: 3;
  widows: 3;
}

.doc-list-item {
  margin-bottom: 0.45em !important;
}

/* Tables: improve print readability + repeat header where present. */
table {
  width: 100%;
  border-collapse: collapse;
  margin: 0.75rem 0 1.2rem;
}
thead {
  display: table-header-group;
}
tfoot {
  display: table-footer-group;
}
th,
td {
  border: 1px solid #e5e7eb;
  padding: 6px 8px;
  vertical-align: top;
}
th {
  background: #f8fafc;
  color: #0f172a;
  font-weight: 700;
}
tr {
  break-inside: avoid;
  page-break-inside: avoid;
}

/* Table of contents: leader dots + page numbers from anchors. */
.doc-toc {
  margin: 1.25rem 0 1.75rem;
  break-after: page;
  page-break-after: always;
}
.doc-toc-title {
  text-align: center !important;
  margin: 0 0 0.75rem !important;
}
.doc-toc-list {
  list-style: none;
  padding: 0;
  margin: 0;
}
.doc-toc-item {
  margin: 0.25rem 0;
}
.doc-toc-link {
  color: inherit;
  text-decoration: none;
  display: block;
}
.doc-toc-link::after {
  content: leader('.') target-counter(attr(href), page);
  color: #64748b;
}
.doc-toc-item--lvl-3 .doc-toc-link { padding-left: 0.75rem; }
.doc-toc-item--lvl-4 .doc-toc-link { padding-left: 1.25rem; }
.doc-toc-item--lvl-5 .doc-toc-link { padding-left: 1.75rem; }
.doc-toc-item--lvl-6 .doc-toc-link { padding-left: 2.25rem; }
"""
    return _PRINT_LAYOUT_CSS_STATIC


# ``</head>`` appears near the start of full documents; avoid ``.lower()`` on multi‑MB HTML.
_HEAD_SEARCH_MAX_CHARS = 768_000


def _find_closing_head_index(html: str) -> int:
    if not html:
        return -1
    if len(html) <= _HEAD_SEARCH_MAX_CHARS:
        return html.lower().rfind("</head>")
    chunk = html[:_HEAD_SEARCH_MAX_CHARS]
    i = chunk.lower().rfind("</head>")
    if i != -1:
        return i
    return html.lower().rfind("</head>")


def warm_weasyprint_static_caches() -> str:
    """Pre-build font/print layout CSS strings and cache them (call from app lifespan)."""
    return _libre_baskerville_font_faces_css() + _print_layout_css()


def inject_print_css(html: str, extra_css: str | None = None) -> str:
    """Insert print/PDF stylesheet before ``</head>`` (append head if missing)."""
    font_and_page = _libre_baskerville_font_faces_css() + _print_layout_css()
    if extra_css:
        font_and_page = font_and_page + "\n" + extra_css
    style_tag = (
        '<style type="text/css" data-translator="weasyprint-print">\n'
        f"{font_and_page}\n"
        "</style>"
    )
    idx = _find_closing_head_index(html)
    if idx != -1:
        return html[:idx] + style_tag + html[idx:]
    return (
        '<!DOCTYPE html><html lang="en"><head><meta charset="utf-8"/>'
        f"{style_tag}</head><body>{html}</body></html>"
    )


def _weasyprint_html_class():
    global _WEASYPRINT_HTML
    if _WEASYPRINT_HTML is not None:
        return _WEASYPRINT_HTML
    try:
        from weasyprint import HTML
    except Exception as e:
        raise RuntimeError(
            "WeasyPrint could not be loaded. Install OS libraries (Pango, Cairo, GObject) "
            "per https://doc.courtbouillon.org/weasyprint/stable/first_steps.html — on "
            "Windows use the GTK3 runtime; in Docker use the backend image apt packages."
        ) from e
    _WEASYPRINT_HTML = HTML
    return _WEASYPRINT_HTML


def html_to_pdf_bytes(
    html: str,
    *,
    base_url: str | None = None,
    inject_css: bool = True,
    extra_print_css: str | None = None,
) -> bytes:
    """Return PDF bytes for a full HTML document string."""
    prepared = inject_print_css(html, extra_print_css) if inject_css else html
    base = base_url if base_url is not None else default_html_base_url()
    HTML = _weasyprint_html_class()
    try:
        return HTML(string=prepared, base_url=base).write_pdf()
    except Exception as e:
        logger.exception("WeasyPrint write_pdf failed")
        raise RuntimeError(f"PDF rendering failed: {e}") from e


def html_to_pdf_file(
    html: str,
    pdf_path: Path,
    *,
    base_url: str | None = None,
    inject_css: bool = True,
    extra_print_css: str | None = None,
) -> Path:
    """Write PDF to ``pdf_path``; parent directories are created."""
    pdf_path = pdf_path.resolve()
    pdf_path.parent.mkdir(parents=True, exist_ok=True)
    data = html_to_pdf_bytes(
        html,
        base_url=base_url,
        inject_css=inject_css,
        extra_print_css=extra_print_css,
    )
    pdf_path.write_bytes(data)
    return pdf_path


_SAFE_FILENAME = re.compile(r"[^A-Za-z0-9._-]+")


def safe_pdf_download_name(filename: str | None) -> str:
    """Sanitize client-provided filename for Content-Disposition."""
    raw = (filename or "document").strip() or "document"
    raw = _SAFE_FILENAME.sub("_", raw).strip("._") or "document"
    if not raw.lower().endswith(".pdf"):
        raw = f"{raw}.pdf"
    return raw[:180]
