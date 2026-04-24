"""Entry: ``python -m app.workers.cleanup_scheduler`` — periodic TTL cleanup."""

from __future__ import annotations

import logging
import sys

from app.workers.cleanup_tasks import cleanup_loop_forever

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    stream=sys.stdout,
)

if __name__ == "__main__":
    cleanup_loop_forever()
