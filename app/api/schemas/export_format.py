"""Shared export format enum for translate + jobs routes."""

from enum import StrEnum


class ExportFormat(StrEnum):
    DOCX = "docx"
    PDF = "pdf"
    BOTH = "both"
