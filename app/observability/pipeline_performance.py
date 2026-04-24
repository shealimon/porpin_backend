"""Pipeline performance breakdown: accumulate stage timings and emit structured reports.

Used for diagnosing bottlenecks only; does not change translation behavior."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

# Stages that sum per-batch wall time while batches may run concurrently — their total can
# exceed pipeline e2e and must not be shown as % of wall-clock or ranked above real stages.
_PARALLEL_AGGREGATE_STAGE_KEYS = frozenset({
    "translation_api_per_batch_wall_sum_s",
})


def _is_parallel_aggregate_stage(key: str) -> bool:
    if key in _PARALLEL_AGGREGATE_STAGE_KEYS:
        return True
    # Chunk workers: several *sum_s* fields add batch-local durations that overlap in real time.
    if key.startswith("chunk_") and "_sum_s" in key:
        return True
    return False


@dataclass
class PipelinePerfReport:
    """In-process timings for a single pipeline run (e.g. monolithic worker or sync)."""

    job_id: str | None = None
    stages: dict[str, float] = field(default_factory=dict)
    meta: dict[str, Any] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def add(self, key: str, seconds: float) -> None:
        self.stages[key] = self.stages.get(key, 0.0) + float(seconds)

    def merge_timings(self, d: dict[str, float] | None) -> None:
        if not d:
            return
        for k, v in d.items():
            self.add(k, v)

    def note(self, text: str) -> None:
        self.notes.append(text)

    def log_structured(
        self,
        *,
        e2e_wall_s: float | None = None,
        log: logging.Logger | None = None,
    ) -> None:
        log = log or logger
        wall_exclusive = {
            k: v for k, v in self.stages.items() if not _is_parallel_aggregate_stage(k)
        }
        parallel_aggregate = {
            k: v for k, v in self.stages.items() if _is_parallel_aggregate_stage(k)
        }
        denom = e2e_wall_s
        if denom is None or denom <= 0:
            denom = sum(wall_exclusive.values())
        rows: list[tuple[str, float, float]] = []
        for name, sec in sorted(wall_exclusive.items(), key=lambda x: -x[1]):
            pct = (100.0 * sec / denom) if denom > 0 else 0.0
            rows.append((name, sec, pct))
        top = rows[:2]
        blob = {
            "kind": "pipeline_perf_report",
            "job_id": self.job_id,
            "e2e_wall_s": e2e_wall_s,
            "denominator_s": denom,
            "denominator_note": (
                "percentages_and_top_bottlenecks_use_wall_clock_stages_only; "
                "parallel_aggregate_* sums overlap in real time and are not additive to e2e"
            ),
            "meta": self.meta,
            "notes": list(self.notes),
            "stages_s": {k: round(v, 4) for k, v in self.stages.items()},
            "stages_pct_of_e2e_wall": {
                k: round(100.0 * v / denom, 2) if denom > 0 else 0.0
                for k, v in wall_exclusive.items()
            },
            "parallel_aggregate_metrics_s": {
                k: round(v, 4) for k, v in parallel_aggregate.items()
            },
            "top_wall_clock_bottlenecks": [
                {"stage": top[i][0], "seconds": round(top[i][1], 4), "pct_of_e2e": round(top[i][2], 2)}
                for i in range(len(top))
            ],
            # Same data as top_wall_clock_bottlenecks / stages_pct_of_e2e_wall (legacy field names).
            "top_bottlenecks": [
                {"stage": top[i][0], "seconds": round(top[i][1], 4), "pct": round(top[i][2], 2)}
                for i in range(len(top))
            ],
            "stages_pct_of_denom": {
                k: round(100.0 * v / denom, 2) if denom > 0 else 0.0
                for k, v in wall_exclusive.items()
            },
        }
        # ensure_ascii=True: Windows consoles often use cp1252; non-ASCII (e.g. approx. sign)
        # in notes or meta would raise UnicodeEncodeError on emit.
        log.info(
            "PIPELINE_PERF_REPORT %s",
            json.dumps(blob, ensure_ascii=True),
        )


def merge_redis_perf_strings(raw: dict[str, str]) -> dict[str, float]:
    """Convert Redis hash string values to floats where possible."""
    out: dict[str, float] = {}
    for k, v in raw.items():
        if v is None:
            continue
        try:
            out[k] = float(v)
        except (TypeError, ValueError):
            continue
    return out


def log_distributed_pipeline_report(
    *,
    job_id: str,
    combined_stages: dict[str, float],
    meta: dict[str, Any],
    notes: list[str],
    e2e_wall_s: float | None,
    log: logging.Logger | None = None,
) -> None:
    """Single structured log line after finalize for sharded (prepare + chunks + finalize) jobs."""
    log = log or logger
    r = PipelinePerfReport(job_id=job_id, stages=dict(combined_stages), meta=meta, notes=list(notes))
    r.log_structured(e2e_wall_s=e2e_wall_s, log=log)


def analyze_parallelism_note(
    *,
    translate_wall_s: float | None,
    translate_api_sum_s: float | None,
    translate_batch_max_concurrency: int | None,
) -> str | None:
    if translate_wall_s is None or translate_api_sum_s is None:
        return None
    if translate_wall_s <= 0:
        return None
    ratio = translate_api_sum_s / translate_wall_s
    if ratio <= 1.01:
        return (
            f"Translation batches are ~serial (api_sum/wall~{ratio:.2f}); "
            "little overlap across batches."
        )
    return (
        f"Translation batches overlap (api_sum/wall~{ratio:.2f}); "
        f"effective concurrency up to ~{translate_batch_max_concurrency or '?'}."
    )
