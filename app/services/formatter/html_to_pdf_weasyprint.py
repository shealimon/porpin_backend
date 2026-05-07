"""Render HTML (from document templates or any full document) to PDF via WeasyPrint.

CSS embedded in ``<style>`` tags is applied. This module injects a print stylesheet
(book-style recto/verso folios, running chapter string, optional A5 trim) inspired by
`CourtBouillon weasyprint-samples <https://github.com/CourtBouillon/weasyprint-samples/tree/main/book>`_.

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

# Shown when WeasyPrint cannot load OS libraries (Ubuntu/Debian hosts without Docker deps).
WEASYPRINT_OSDEPS_INSTALL_HINT = (
    "Themed PDFs need WeasyPrint system libraries on the API host (Pango/Cairo/GDK pixbuf). "
    "On Ubuntu/EC2 run `sudo bash scripts/install-weasyprint-deps-ubuntu.sh` from your deployed "
    "`backend/` checkout (packages also listed in Dockerfile). Docs: "
    "https://doc.courtbouillon.org/weasyprint/stable/first_steps.html "
    "After apt install, restart **gunicorn** and **all RQ translation workers** (finalize runs PDF in "
    "those processes: `python -m app.workers.rq_worker`). Until fixed, exports fall back to "
    "DOCX→PDF (plain-looking)."
)


def default_html_base_url() -> str:
    """Directory URI for resolving relative assets (fonts, optional linked CSS)."""
    u = _APP_DIR.as_uri()
    return u if u.endswith("/") else f"{u}/"


_LIBRE_BASKERVILLE_FACES_CSS: str | None = None
_PRINT_LAYOUT_CACHE: dict[str, str] = {}
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


def _build_modern_print_css(*, page_size: str) -> str:
    """Premium / illustrated-style book (WeasyPrint sample ``book.css`` family): bottom folios, A4/A5."""
    key = (page_size or "a4").strip().lower()
    if key not in ("a4", "a5"):
        key = "a4"
    size_decl = "size: A4;" if key == "a4" else "size: 148mm 210mm;"
    bottom_margin = "30mm" if key == "a4" else "26mm"
    css = (
        """
@page {
  """
        + size_decl
        + """
  margin: 18mm 16mm """
        + bottom_margin
        + """ 16mm;
  @top-center {
    content: string(doc_title);
    font-size: 9pt;
    color: #64748b;
    font-family: "Libre Baskerville", "Segoe UI", "DejaVu Sans", sans-serif;
  }
}

/* Book-style recto/verso folios (cf. weasyprint-samples book). */
@page :left {
  @bottom-left {
    content: counter(page);
    font-size: 9pt;
    color: #64748b;
    font-family: "Libre Baskerville", "Segoe UI", "DejaVu Sans", sans-serif;
  }
  @bottom-right {
    content: string(chapter_title);
    font-size: 8.5pt;
    color: #94a3b8;
    font-style: italic;
    font-family: "Libre Baskerville", "Segoe UI", "DejaVu Sans", sans-serif;
  }
}

@page :right {
  @bottom-right {
    content: counter(page);
    font-size: 9pt;
    color: #64748b;
    font-family: "Libre Baskerville", "Segoe UI", "DejaVu Sans", sans-serif;
  }
  @bottom-left {
    content: string(chapter_title);
    font-size: 8.5pt;
    color: #94a3b8;
    font-style: italic;
    font-family: "Libre Baskerville", "Segoe UI", "DejaVu Sans", sans-serif;
  }
}

@page :first {
  @top-center { content: none; }
  @bottom-left { content: none; }
  @bottom-right { content: none; }
}

@page chapter {
  @top-center { content: none; }
  @bottom-left { content: none; }
  @bottom-right { content: none; }
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

.doc-subtitle {
  text-align: center;
  font-family: "Libre Baskerville", "Georgia", "Times New Roman", serif;
  font-size: 13.5pt !important;
  line-height: 1.45 !important;
  color: #475569 !important;
  margin: 0.35rem auto 1.35rem !important;
  max-width: 140mm;
  font-style: italic;
  font-weight: 400;
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
 * Chapter openers: always start on a fresh page (not forced recto).
 * ``break-before: right`` inserts a blank verso whenever the prior page is already recto —
 * common complaint in single-column PDFs; ``page`` avoids that while keeping a clear break.
 */
section.doc-chapter,
.doc-chapter-page {
  break-before: page;
  page-break-before: always;
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
  font-weight: 800;
  line-height: 1;
  letter-spacing: -0.03em;
  color: #0f172a;
  margin: 0 0 6mm;
}

.doc-chapter-number strong {
  font-weight: 800;
  letter-spacing: inherit;
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
  string-set: chapter_title content(text);
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

/* Subheads: h5 + class only — no doc-heading--N tier conflict */
.doc-heading.doc-heading--sub,
h5.doc-heading--sub {
  font-size: 11.25pt !important;
  font-weight: 500 !important;
  font-style: italic !important;
  color: #64748b !important;
  margin-top: 0.95rem !important;
  margin-bottom: 0.42rem !important;
  letter-spacing: 0.01em !important;
}

.doc-paragraph {
  font-size: 11.5pt !important;
  line-height: 1.68 !important;
  color: #171717 !important;
  text-align: justify !important;
  hyphens: auto;
  white-space: normal;
  word-break: normal;
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

.doc-paragraph--quote {
  margin-left: 1.35rem !important;
  margin-right: 0.75rem !important;
  padding-left: 1rem !important;
  border-left: 3px solid #cbd5e1 !important;
  font-style: italic !important;
  color: #334155 !important;
  text-indent: 0 !important;
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
    )
    return css


def _build_classical_print_css() -> str:
    """Traditional trade layout (WeasyPrint sample ``book-classical.css``): top running heads, small trim.

    Uses ``110mm × 170mm`` like the upstream classical sample; body typography is serif-first
    with first-line paragraph indents and small-caps running chapter titles.
    """
    return """
@page {
  size: 110mm 170mm;
}

@page :left {
  margin: 20mm 10mm 14mm 15mm;
  @top-left {
    content: counter(page);
    font-size: 9pt;
    font-family: "Libre Baskerville", "Georgia", "Times New Roman", serif;
    color: #475569;
  }
  @top-right {
    content: string(chapter_title);
    font-size: 9pt;
    font-variant: small-caps;
    font-weight: 400;
    letter-spacing: 0.06em;
    font-family: "Libre Baskerville", "Georgia", "Times New Roman", serif;
    color: #64748b;
  }
}

@page :right {
  margin: 20mm 15mm 14mm 10mm;
  @top-left {
    content: string(chapter_title);
    font-size: 9pt;
    font-variant: small-caps;
    font-weight: 400;
    letter-spacing: 0.06em;
    font-family: "Libre Baskerville", "Georgia", "Times New Roman", serif;
    color: #64748b;
  }
  @top-right {
    content: counter(page);
    font-size: 9pt;
    font-family: "Libre Baskerville", "Georgia", "Times New Roman", serif;
    color: #475569;
  }
}

@page :blank {
  @top-left { content: none; }
  @top-right { content: none; }
}

@page clean {
  @top-left { content: none; }
  @top-right { content: none; }
}

@page :first {
  @top-left { content: none; }
  @top-right { content: none; }
}

@page chapter {
  @top-left { content: none; }
  @top-right { content: none; }
}

.doc-root {
  min-height: auto !important;
  background: #ffffff !important;
  -webkit-font-smoothing: auto;
  font-size: 10pt !important;
  line-height: 1.55 !important;
  color: #1a1a1a !important;
}

.doc-title:not(.doc-title--default) {
  string-set: doc_title content(text);
}

.doc-landmark .doc-title {
  font-size: 1.85rem !important;
  line-height: 1.25 !important;
  font-weight: 400 !important;
  letter-spacing: 0.02em !important;
  color: #0f172a !important;
  margin: 2.5em 0 1em !important;
}

.doc-subtitle {
  text-align: center;
  font-family: "Libre Baskerville", "Georgia", "Times New Roman", serif;
  font-size: 11pt !important;
  font-style: italic !important;
  font-weight: 400 !important;
  color: #475569 !important;
  margin: 0.25rem auto 1.5rem !important;
  max-width: 95mm;
  text-indent: 0 !important;
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

/* Classical: fresh page per chapter without forced recto/verso (avoids empty interstitial pages). */
section.doc-chapter,
.doc-chapter-page {
  break-before: page;
  page-break-before: always;
  break-after: avoid;
  page-break-after: avoid;
  padding-top: 8mm;
}

.doc-chapter-opener {
  page: chapter;
  display: block;
  text-align: center;
  padding-top: 18mm;
  padding-bottom: 6mm;
}

.doc-chapter-number {
  font-family: "Libre Baskerville", "Georgia", "Times New Roman", serif;
  font-size: 40pt;
  font-weight: 700;
  line-height: 1;
  letter-spacing: -0.02em;
  color: #0f172a;
  margin: 0 0 4mm;
}

.doc-chapter-number strong {
  font-weight: 700;
}

.doc-chapter-ornament {
  display: block;
  width: 22mm;
  height: 1px;
  margin: 0 auto 5mm;
  background: #94a3b8;
  opacity: 0.55;
}

.doc-chapter-title {
  string-set: chapter_title content(text);
  font-size: 16pt !important;
  line-height: 1.2 !important;
  font-variant: small-caps !important;
  font-weight: 400 !important;
  letter-spacing: 0.08em !important;
  text-align: center !important;
  color: #0f172a !important;
  margin: 0 auto !important;
  max-width: 92mm;
}

.doc-chapter-start,
section.doc-chapter > h2.doc-heading--2:first-of-type {
  string-set: chapter_title content(text);
  text-align: center !important;
  font-size: 12pt !important;
  font-variant: small-caps !important;
  font-weight: 400 !important;
  letter-spacing: 0.06em !important;
  margin: 0.5em 0 1em !important;
}

.doc-heading {
  break-after: avoid;
  page-break-after: avoid;
}

.doc-heading--3 {
  font-size: 11pt !important;
  font-variant: small-caps !important;
  font-weight: 400 !important;
  letter-spacing: 0.05em !important;
  color: #0f172a !important;
  margin: 1.5em 0 0.75em !important;
}

.doc-heading--4,
.doc-heading--5 {
  font-size: 10.5pt !important;
  font-variant: small-caps !important;
  font-weight: 400 !important;
  color: #334155 !important;
  margin: 1.2em 0 0.55em !important;
}

.doc-heading--6 {
  font-size: 10pt !important;
  font-style: italic !important;
  font-weight: 500 !important;
  color: #475569 !important;
  margin: 1em 0 0.45em !important;
}

.doc-heading.doc-heading--sub,
h5.doc-heading--sub {
  font-size: 10pt !important;
  font-style: italic !important;
  font-variant: normal !important;
  font-weight: 500 !important;
  color: #64748b !important;
  margin: 0.9rem 0 0.35rem !important;
}

.doc-paragraph {
  font-size: 10pt !important;
  line-height: 1.55 !important;
  color: #1a1a1a !important;
  text-align: justify !important;
  hyphens: auto;
  text-indent: 1em !important;
  margin: 0 0 0.35em !important;
  orphans: 3;
  widows: 3;
}

h2 + .doc-paragraph,
h3 + .doc-paragraph,
h4 + .doc-paragraph,
h5 + .doc-paragraph,
h6 + .doc-paragraph {
  text-indent: 0 !important;
}

.doc-paragraph + .doc-paragraph {
  margin-top: 0 !important;
}

.doc-paragraph--quote {
  margin: 0.5em 0 0.5em 1em !important;
  padding-left: 0.75em !important;
  border-left: 2px solid #cbd5e1 !important;
  font-style: italic !important;
  text-indent: 0 !important;
}

.doc-list {
  font-size: 10pt !important;
  line-height: 1.5 !important;
  margin: 0.5em 0 0.75em !important;
}

.doc-list-item {
  margin-bottom: 0.35em !important;
}

table {
  width: 100%;
  border-collapse: collapse;
  margin: 0.65rem 0 1rem;
  font-size: 9.5pt !important;
}
thead { display: table-header-group; }
tfoot { display: table-footer-group; }
th,
td {
  border: 1px solid #e5e7eb;
  padding: 5px 6px;
  vertical-align: top;
}
th {
  background: #f8fafc;
  font-weight: 600;
}
tr {
  break-inside: avoid;
  page-break-inside: avoid;
}

.doc-toc {
  margin: 1rem 0 1.5rem;
  page: clean;
  break-after: page;
  page-break-after: always;
}
.doc-toc-title {
  text-align: center !important;
  font-variant: small-caps !important;
  letter-spacing: 0.06em !important;
  margin: 0 0 0.75rem !important;
}
.doc-toc-list {
  list-style: none;
  padding: 0;
  margin: 0;
}
.doc-toc-item {
  margin: 0.2rem 0;
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


def _print_layout_css(*, page_size: str = "a4", book_preset: str = "modern") -> str:
    """Print rules: ``modern`` = sample *book* style; ``classical`` = sample *book-classical* style."""
    pr = (book_preset or "modern").strip().lower()
    if pr not in ("modern", "classical"):
        pr = "modern"
    ps = (page_size or "a4").strip().lower()
    if ps not in ("a4", "a5"):
        ps = "a4"
    cache_key = f"{pr}:{ps}" if pr == "modern" else "classical"
    cached = _PRINT_LAYOUT_CACHE.get(cache_key)
    if cached is not None:
        return cached
    if pr == "classical":
        css = _build_classical_print_css()
    else:
        css = _build_modern_print_css(page_size=ps)
    _PRINT_LAYOUT_CACHE[cache_key] = css
    return css


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
    """Pre-build font + print CSS caches for common variants (call from app lifespan)."""
    _ = _print_layout_css(page_size="a5")
    _ = _print_layout_css(book_preset="classical")
    return _libre_baskerville_font_faces_css() + _print_layout_css(page_size="a4")


def inject_print_css(
    html: str,
    extra_css: str | None = None,
    *,
    page_size: str | None = None,
    book_preset: str | None = None,
) -> str:
    """Insert print/PDF stylesheet before ``</head>`` (append head if missing)."""
    from app.core.pipeline_settings import get_pipeline_settings

    settings = get_pipeline_settings()
    ps = page_size if page_size is not None else settings.weasyprint_pdf_page_size
    bp = book_preset if book_preset is not None else settings.weasyprint_book_preset
    font_and_page = (
        _libre_baskerville_font_faces_css()
        + _print_layout_css(page_size=ps, book_preset=bp)
    )
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
            "Windows use the GTK3 runtime; in Docker use the backend image apt packages. "
            f"{WEASYPRINT_OSDEPS_INSTALL_HINT}"
        ) from e
    _WEASYPRINT_HTML = HTML
    return _WEASYPRINT_HTML


def html_to_pdf_bytes(
    html: str,
    *,
    base_url: str | None = None,
    inject_css: bool = True,
    extra_print_css: str | None = None,
    page_size: str | None = None,
    book_preset: str | None = None,
) -> bytes:
    """Return PDF bytes for a full HTML document string.

    ``page_size`` overrides :envvar:`WEASYPRINT_PDF_PAGE_SIZE` (``a4`` or ``a5``) for this call only.
    ``book_preset`` overrides :envvar:`WEASYPRINT_BOOK_PRESET` (``modern`` or ``classical``).
    """
    prepared = (
        inject_print_css(
            html,
            extra_print_css,
            page_size=page_size,
            book_preset=book_preset,
        )
        if inject_css
        else html
    )
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
    page_size: str | None = None,
    book_preset: str | None = None,
) -> Path:
    """Write PDF to ``pdf_path``; parent directories are created."""
    pdf_path = pdf_path.resolve()
    pdf_path.parent.mkdir(parents=True, exist_ok=True)
    data = html_to_pdf_bytes(
        html,
        base_url=base_url,
        inject_css=inject_css,
        extra_print_css=extra_print_css,
        page_size=page_size,
        book_preset=book_preset,
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
