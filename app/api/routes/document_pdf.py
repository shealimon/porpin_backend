"""POST HTML → PDF (WeasyPrint) for template-generated documents."""

from __future__ import annotations

import asyncio
import logging
from typing import Self

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field, model_validator

from app.limiter import limiter, user_or_ip_key
from app.services.document_template_render import render_document_html
from app.services.document_template_render.models import DocumentForTemplate
from app.services.formatter.html_to_pdf_weasyprint import (
    html_to_pdf_bytes,
    safe_pdf_download_name,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/document", tags=["document"])

_MAX_HTML_CHARS = 5_000_000


class HtmlToPdfRequest(BaseModel):
    """Either a full ``html`` string, or a structured ``document`` + optional template."""

    html: str | None = Field(
        default=None,
        max_length=_MAX_HTML_CHARS,
        description="Full HTML (e.g. from ``renderDocumentHtml`` in the app). Use ``document`` instead for server-side theming.",
    )
    document: DocumentForTemplate | None = Field(
        default=None,
        description="Structured content; styled with ``template_type`` (default: report).",
    )
    template_type: str | None = Field(
        default=None,
        description=(
            "Document theme: ebook, report, minimal, blog, academic, or bilingual. "
            "Omitted or invalid values default to report."
        ),
    )
    filename: str | None = Field(
        default=None,
        description="Optional download name; .pdf is appended when missing.",
    )
    base_url: str | None = Field(
        default=None,
        description=(
            "Optional base URL (file: or https:) so link href and CSS url() resolve. "
            "Defaults to the API app directory for bundled fonts."
        ),
    )
    pdf_page_size: str | None = Field(
        default=None,
        description="Optional PDF trim: `a4` or `a5` (trade paperback). Uses server default from WEASYPRINT_PDF_PAGE_SIZE when omitted.",
    )
    pdf_book_preset: str | None = Field(
        default=None,
        description=(
            "Optional book layout: `modern` (premium, WeasyPrint *book.css* style) or `classical` "
            "(*book-classical.css*, fixed small trim). Uses WEASYPRINT_BOOK_PRESET when omitted."
        ),
    )

    @model_validator(mode="after")
    def _html_xor_document(self) -> Self:
        if self.document is not None:
            if self.html is not None and self.html.strip() != "":
                raise ValueError("Provide only one of `html` or `document`.")
            return self
        if self.html is not None and len(self.html.strip()) > 0:
            return self
        raise ValueError("Provide either non-empty `html` or a `document` object.")


@router.post("/html-to-pdf")
@limiter.limit("20/minute", key_func=user_or_ip_key)
async def html_to_pdf(request: Request, body: HtmlToPdfRequest) -> Response:
    """Convert HTML to PDF with print margins, section breaks, and footer page numbers.

    If ``document`` is set, the server applies the selected template (``template_type``)
    and renders HTML before conversion — same content model as the in-app document templates.
    """
    _ = request
    if body.document is not None:
        html = await asyncio.to_thread(
            render_document_html,
            body.document,
            body.template_type,
        )
    else:
        html = body.html
        assert html is not None
    try:
        pdf_bytes = await asyncio.to_thread(
            html_to_pdf_bytes,
            html,
            base_url=body.base_url,
            page_size=body.pdf_page_size,
            book_preset=body.pdf_book_preset,
        )
    except RuntimeError as e:
        detail = str(e)
        logger.warning("html-to-pdf failed: %s", detail)
        raise HTTPException(
            status_code=503,
            detail=detail,
        ) from e
    name = safe_pdf_download_name(body.filename)
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="{name}"',
        },
    )
