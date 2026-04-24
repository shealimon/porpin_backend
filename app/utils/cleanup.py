"""Post-response file removal helpers."""

from __future__ import annotations

import logging
from pathlib import Path

from app.services.storage.local_storage import delete_file

logger = logging.getLogger(__name__)


def safe_delete_many(paths: list[Path | str]) -> None:
    for p in paths:
        try:
            delete_file(p)
        except OSError as e:
            logger.debug("Cleanup omit %s: %s", p, e)
