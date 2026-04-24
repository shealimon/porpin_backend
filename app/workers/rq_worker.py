"""Run: ``python -m app.workers.rq_worker`` (RQ 1.x; timer-based timeouts on Windows)."""

from __future__ import annotations

import logging
import sys

from redis import Redis
from rq import Connection, Worker
from rq.timeouts import TimerDeathPenalty
from rq.worker import SimpleWorker

from app.core.pipeline_settings import get_pipeline_settings

logger = logging.getLogger(__name__)


class WindowsSimpleWorker(SimpleWorker):
    """Same as SimpleWorker but uses thread timers instead of SIGALRM (Unix-only)."""

    death_penalty_class = TimerDeathPenalty


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )
    settings = get_pipeline_settings()
    if not settings.redis_url:
        raise SystemExit("REDIS_URL is not set.")
    conn = Redis.from_url(settings.redis_url)
    worker_cls = WindowsSimpleWorker if sys.platform == "win32" else Worker
    with Connection(conn):
        w = worker_cls(
            [settings.rq_high_priority_queue_name, settings.rq_queue_name]
        )
        logger.info(
            "RQ worker listening on queues %s then %s "
            "(sharded mode also needs: python -m app.workers.chunk_worker)",
            settings.rq_high_priority_queue_name,
            settings.rq_queue_name,
        )
        w.work()


if __name__ == "__main__":
    main()
