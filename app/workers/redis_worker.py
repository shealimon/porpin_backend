"""
Deprecated: use RQ instead — ``python -m app.workers.rq_worker``.

This module used a raw Redis list (BRPOP). Jobs are now processed via RQ.
"""

from __future__ import annotations

import logging
import sys

logger = logging.getLogger(__name__)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        stream=sys.stdout,
    )
    logger.error(
        "redis_worker is deprecated. Run: python -m app.workers.rq_worker"
    )
    raise SystemExit(2)


if __name__ == "__main__":
    main()
