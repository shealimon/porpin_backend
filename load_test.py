#!/usr/bin/env python3
"""
Load test: concurrent POST /translate with file upload (multipart ``file`` field).

Usage:
  cd backend
  python load_test.py --users 20 --bearer YOUR_SUPABASE_JWT
  python load_test.py --users 20 --api-key YOUR_DEV_API_KEY
  python load_test.py --presets --bearer YOUR_JWT --quiet --json-report report.json

  Values are read from backend/.env if present (python-dotenv), for example:

    LOAD_TEST_URL=http://127.0.0.1:8000/translate
    LOAD_TEST_USERS=20
    LOAD_TEST_FILE=sample.pdf
    LOAD_TEST_BEARER=eyJ...       # full JWT
    LOAD_TEST_API_KEY=...         # optional; or use SEED_API_KEY (same as API)

  CLI flags override .env.

**Load-test server settings** (staging only): set ``HTTP_RATE_LIMIT_ENABLED=false`` so
SlowAPI's 300/min cap does not dominate results when simulating 100–1000 concurrent users.
Use ``SLOW_REQUEST_LOG_THRESHOLD_S=3`` (default) to log slow requests on the API.

Without auth, /translate returns 401 (unless allow_anonymous_jobs is enabled on the API).

Use the full Supabase **access_token** (JWT with three dot-separated parts). A truncated
string like ``eyJ...`` with literal ``...`` or copy-paste from docs will always get 401.

Requires: httpx (pip install httpx)
Place a sample PDF at FILE_PATH or pass --file path/to/sample.pdf

The API accepts a file upload only (no separate ``text`` form field). Use a small PDF/DOCX/TXT
as the sample payload.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import asyncio
import statistics
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import httpx
from dotenv import load_dotenv

_BACKEND_DIR = Path(__file__).resolve().parent
load_dotenv(_BACKEND_DIR / ".env")

# --- Configuration (defaults; override via CLI or run_test kwargs) ---
API_URL = os.environ.get(
    "LOAD_TEST_URL",
    "http://127.0.0.1:8000/translate",
)
_file_default = os.environ.get("LOAD_TEST_FILE")
FILE_PATH = (
    Path(_file_default)
    if _file_default
    else _BACKEND_DIR / "sample.pdf"
)
if not FILE_PATH.is_absolute():
    FILE_PATH = (_BACKEND_DIR / FILE_PATH).resolve()
DEFAULT_TIMEOUT_S = 120.0
MAX_RETRIES = 2
RETRY_BASE_DELAY_S = 0.75

TrafficMode = Literal["burst", "stagger"]


@dataclass
class RequestLog:
    index: int
    status_code: int | None
    elapsed_s: float
    ok: bool
    error: str | None = None


@dataclass
class RunSummary:
    total: int = 0
    success: int = 0
    failure: int = 0
    total_wall_s: float = 0.0
    """Latencies for successful (2xx) responses only."""
    response_times_s: list[float] = field(default_factory=list)
    """Latencies for every finished request (success or failure)."""
    all_elapsed_s: list[float] = field(default_factory=list)
    logs: list[RequestLog] = field(default_factory=list)
    concurrent_users: int = 0

    def error_rate(self) -> float:
        if self.total <= 0:
            return 0.0
        return self.failure / self.total

    def throughput_success_rps(self) -> float:
        """Successful requests per second over total wall-clock time."""
        if self.total_wall_s <= 0:
            return 0.0
        return self.success / self.total_wall_s

    def throughput_attempted_rps(self) -> float:
        """All completed attempts per second (includes failures)."""
        if self.total_wall_s <= 0:
            return 0.0
        return self.total / self.total_wall_s

    def stats_dict(self) -> dict:
        """Serializable metrics for ``--json-report``."""
        ok = sorted(self.response_times_s)
        all_e = sorted(self.all_elapsed_s)
        return {
            "concurrent_users": self.concurrent_users,
            "total_requests": self.total,
            "success": self.success,
            "failure": self.failure,
            "error_rate": round(self.error_rate(), 6),
            "total_wall_s": round(self.total_wall_s, 6),
            "throughput_success_rps": round(self.throughput_success_rps(), 4),
            "throughput_attempted_rps": round(self.throughput_attempted_rps(), 4),
            "latency_2xx_s": {
                "avg": round(statistics.mean(ok), 6) if ok else None,
                "p50": round(_percentile(ok, 50), 6) if ok else None,
                "p95": round(_percentile(ok, 95), 6) if ok else None,
                "p99": round(_percentile(ok, 99), 6) if ok else None,
                "min": round(min(ok), 6) if ok else None,
                "max": round(max(ok), 6) if ok else None,
            },
            "latency_all_s": {
                "avg": round(statistics.mean(all_e), 6) if all_e else None,
                "p95": round(_percentile(all_e, 95), 6) if all_e else None,
                "p99": round(_percentile(all_e, 99), 6) if all_e else None,
            },
        }

    def print_report(self, *, title: str = "RESULT SUMMARY") -> None:
        print()
        print("=" * 52)
        print(title)
        print("=" * 52)
        print(f"  Concurrent users:   {self.concurrent_users}")
        print(f"  Total requests:     {self.total}")
        print(f"  Success:            {self.success}")
        print(f"  Failure:            {self.failure}")
        print(f"  Error rate:         {self.error_rate() * 100:.2f}%")
        print(f"  Total wall time:    {self.total_wall_s:.3f}s")
        print(
            f"  Throughput (2xx): {self.throughput_success_rps():.2f} req/s  "
            f"(attempted: {self.throughput_attempted_rps():.2f} req/s)"
        )
        if self.all_elapsed_s:
            avg_all = statistics.mean(self.all_elapsed_s)
            print(f"  Avg response (all): {avg_all:.3f}s")
            print(
                f"  p95 / p99 (all):    {_percentile(sorted(self.all_elapsed_s), 95):.3f}s / "
                f"{_percentile(sorted(self.all_elapsed_s), 99):.3f}s"
            )
            print(
                f"  Min / Max (all):    {min(self.all_elapsed_s):.3f}s / "
                f"{max(self.all_elapsed_s):.3f}s"
            )
        if self.response_times_s:
            ok = sorted(self.response_times_s)
            avg_ok = statistics.mean(self.response_times_s)
            print(f"  Avg response (2xx): {avg_ok:.3f}s")
            print(
                f"  p50 / p95 / p99:    {_percentile(ok, 50):.3f}s / "
                f"{_percentile(ok, 95):.3f}s / {_percentile(ok, 99):.3f}s"
            )
            print(
                f"  Min / Max (2xx):    {min(self.response_times_s):.3f}s / "
                f"{max(self.response_times_s):.3f}s"
            )
        print("=" * 52)


def _percentile(values: list[float], p: float) -> float:
    """Linear interpolation percentile (p in 0..100)."""
    if not values:
        return 0.0
    xs = sorted(values)
    if len(xs) == 1:
        return xs[0]
    k = (len(xs) - 1) * (p / 100.0)
    f = math.floor(k)
    c = math.ceil(k)
    if f == c:
        return xs[int(k)]
    return xs[f] + (xs[c] - xs[f]) * (k - f)


def _should_retry(status: int | None, exc: BaseException | None) -> bool:
    if exc is not None:
        return isinstance(
            exc,
            (
                httpx.TimeoutException,
                httpx.ConnectError,
                httpx.ReadError,
                httpx.RemoteProtocolError,
            ),
        )
    if status is None:
        return True
    return status in (429, 502, 503, 504)


def _error_detail_from_response(r: httpx.Response) -> str:
    """FastAPI usually returns {\"detail\": \"...\"}."""
    try:
        data = r.json()
        d = data.get("detail")
        if d is None:
            return json.dumps(data)[:220]
        if isinstance(d, list):
            return str(d)[:220]
        return str(d)[:220]
    except Exception:
        t = (r.text or "").strip()
        return t[:220] if t else f"HTTP {r.status_code}"


def _warn_if_bearer_not_full_jwt(bearer: str | None) -> None:
    if not bearer or not str(bearer).strip():
        return
    token = str(bearer).strip()
    if token.lower().startswith("bearer "):
        token = token[7:].strip()
    if "..." in token or token.count(".") < 2:
        print(
            "WARNING: --bearer does not look like a complete JWT "
            "(expected 3 segments separated by dots). "
            "Use the full access_token from Supabase session, not a shortened example."
        )


def build_auth_headers(
    bearer: str | None,
    api_key: str | None,
) -> dict[str, str]:
    """Headers for Supabase JWT or dev API key (same as browser / frontend)."""
    h: dict[str, str] = {}
    if bearer:
        b = bearer.strip()
        if not b.lower().startswith("bearer "):
            b = f"Bearer {b}"
        h["Authorization"] = b
    if api_key:
        h["X-API-Key"] = api_key.strip()
    return h


def _append_slow_log(path: Path, line: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def _mime_for_filename(name: str) -> str:
    lower = name.lower()
    if lower.endswith(".pdf"):
        return "application/pdf"
    if lower.endswith(".docx"):
        return (
            "application/vnd.openxmlformats-officedocument."
            "wordprocessingml.document"
        )
    if lower.endswith(".txt"):
        return "text/plain"
    return "application/octet-stream"


def _maybe_log_slow(
    *,
    path: Path | None,
    lock: threading.Lock | None,
    threshold_s: float | None,
    elapsed: float,
    index: int,
    status: int | None,
    ok: bool,
) -> None:
    if path is None or threshold_s is None or elapsed < threshold_s:
        return
    line = (
        f"{time.strftime('%Y-%m-%dT%H:%M:%S')}Z index={index} "
        f"elapsed_s={elapsed:.4f} status={status} ok={ok}"
    )
    if lock is not None:
        with lock:
            _append_slow_log(path, line)
    else:
        _append_slow_log(path, line)


def _one_request_sync(
    client: httpx.Client,
    url: str,
    file_name: str,
    file_bytes: bytes,
    index: int,
    timeout: float,
    max_retries: int,
    headers: dict[str, str] | None = None,
    params: dict[str, str] | None = None,
    verbose: bool = True,
    slow_threshold_s: float | None = None,
    slow_log_path: Path | None = None,
    slow_log_lock: threading.Lock | None = None,
) -> RequestLog:
    t0 = time.perf_counter()
    last_status: int | None = None
    last_err: str | None = None
    files = {"file": (file_name, file_bytes, _mime_for_filename(file_name))}

    for attempt in range(max_retries + 1):
        try:
            r = client.post(
                url,
                files=files,
                headers=headers,
                timeout=timeout,
                params=params or None,
            )
            elapsed = time.perf_counter() - t0
            ok = 200 <= r.status_code < 300
            last_status = r.status_code
            detail = _error_detail_from_response(r) if not ok else None
            _maybe_log_slow(
                path=slow_log_path,
                lock=slow_log_lock,
                threshold_s=slow_threshold_s,
                elapsed=elapsed,
                index=index,
                status=r.status_code,
                ok=ok,
            )

            if ok:
                if verbose:
                    print(
                        f"  [{index:4d}] status={r.status_code} "
                        f"time={elapsed:.3f}s attempt={attempt + 1}"
                    )
                return RequestLog(
                    index=index,
                    status_code=r.status_code,
                    elapsed_s=elapsed,
                    ok=True,
                    error=None,
                )

            last_err = detail or f"HTTP {r.status_code}"
            if attempt < max_retries and _should_retry(r.status_code, None):
                delay = RETRY_BASE_DELAY_S * (2**attempt)
                if verbose:
                    print(
                        f"  [{index:4d}] status={r.status_code} retry in {delay:.2f}s "
                        f"(attempt {attempt + 1}) | {detail}"
                    )
                time.sleep(delay)
                continue

            if verbose:
                print(
                    f"  [{index:4d}] status={r.status_code} "
                    f"time={elapsed:.3f}s (final) | {detail}"
                )
            return RequestLog(
                index=index,
                status_code=r.status_code,
                elapsed_s=elapsed,
                ok=False,
                error=last_err,
            )

        except Exception as e:
            last_err = str(e)
            elapsed = time.perf_counter() - t0
            _maybe_log_slow(
                path=slow_log_path,
                lock=slow_log_lock,
                threshold_s=slow_threshold_s,
                elapsed=elapsed,
                index=index,
                status=last_status,
                ok=False,
            )
            if attempt < max_retries and _should_retry(None, e):
                delay = RETRY_BASE_DELAY_S * (2**attempt)
                if verbose:
                    print(
                        f"  [{index:4d}] error={last_err!r} retry in {delay:.2f}s"
                    )
                time.sleep(delay)
                continue
            if verbose:
                print(
                    f"  [{index:4d}] FAILED time={elapsed:.3f}s err={last_err}"
                )
            return RequestLog(
                index=index,
                status_code=last_status,
                elapsed_s=elapsed,
                ok=False,
                error=last_err,
            )

    elapsed = time.perf_counter() - t0
    if verbose:
        print(
            f"  [{index:4d}] FAILED status={last_status} "
            f"time={elapsed:.3f}s err={last_err}"
        )
    return RequestLog(
        index=index,
        status_code=last_status,
        elapsed_s=elapsed,
        ok=False,
        error=last_err,
    )


def run_test(
    num_users: int,
    *,
    api_url: str = API_URL,
    file_path: Path | str = FILE_PATH,
    mode: TrafficMode = "burst",
    stagger_delay_s: float = 0.25,
    max_start_rate_per_s: float | None = None,
    max_retries: int = MAX_RETRIES,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    use_async: bool = False,
    bearer: str | None = None,
    api_key: str | None = None,
    query_params: dict[str, str] | None = None,
    verbose: bool = True,
    slow_threshold_s: float | None = 3.0,
    slow_log_path: Path | None = None,
) -> RunSummary:
    """
    Run concurrent upload load test.

    Args:
        num_users: number of concurrent simulated users (requests).
        api_url: full URL to POST /translate.
        file_path: path to PDF (or other allowed) file to upload.
        mode: "burst" = all workers start together (after optional rate spacing);
              "stagger" = each worker sleeps index * stagger_delay_s before request.
        stagger_delay_s: delay between staggered starts (mode=stagger).
        max_start_rate_per_s: optional max starts per second (e.g. 10); None = unlimited.
        max_retries: retries for transient failures / 429 / 5xx.
        timeout_s: per-request timeout.
        use_async: if True, use asyncio + httpx.AsyncClient (single event loop).
        bearer: Supabase access token (raw JWT or full 'Bearer ...' string).
        api_key: optional X-API-Key for dev (if enabled on server).
        query_params: optional query string (default ``export=docx``).
        verbose: print one line per request when True.
        slow_threshold_s: log rows to ``slow_log_path`` when round-trip time exceeds this;
            ignored if ``slow_log_path`` is None. Pass None to disable client slow-log.
        slow_log_path: optional path to append slow-request lines from the client.
    """
    _warn_if_bearer_not_full_jwt(bearer)
    auth_headers = build_auth_headers(bearer, api_key)
    path = Path(file_path)
    if not path.is_file():
        raise FileNotFoundError(
            f"Upload file not found: {path.resolve()}\n"
            "Add sample.pdf next to this script or set FILE_PATH / --file."
        )

    file_bytes = path.read_bytes()
    file_name = path.name
    qp = {"export": "docx"} if query_params is None else dict(query_params)

    if use_async:
        return asyncio.run(
            _run_async(
                num_users=num_users,
                api_url=api_url,
                file_name=file_name,
                file_bytes=file_bytes,
                mode=mode,
                stagger_delay_s=stagger_delay_s,
                max_start_rate_per_s=max_start_rate_per_s,
                max_retries=max_retries,
                timeout_s=timeout_s,
                auth_headers=auth_headers,
                query_params=qp,
                verbose=verbose,
                slow_threshold_s=slow_threshold_s,
                slow_log_path=slow_log_path,
            )
        )

    return _run_threaded(
        num_users=num_users,
        api_url=api_url,
        file_name=file_name,
        file_bytes=file_bytes,
        mode=mode,
        stagger_delay_s=stagger_delay_s,
        max_start_rate_per_s=max_start_rate_per_s,
        max_retries=max_retries,
        timeout_s=timeout_s,
        auth_headers=auth_headers,
        query_params=qp,
        verbose=verbose,
        slow_threshold_s=slow_threshold_s,
        slow_log_path=slow_log_path,
    )


def _run_threaded(
    num_users: int,
    api_url: str,
    file_name: str,
    file_bytes: bytes,
    mode: TrafficMode,
    stagger_delay_s: float,
    max_start_rate_per_s: float | None,
    max_retries: int,
    timeout_s: float,
    auth_headers: dict[str, str],
    query_params: dict[str, str],
    verbose: bool,
    slow_threshold_s: float | None,
    slow_log_path: Path | None,
) -> RunSummary:
    summary = RunSummary(concurrent_users=num_users)
    wall0 = time.perf_counter()
    rate_lock = threading.Lock()
    next_slot = [0.0]  # mutable for closure
    slow_log_lock = threading.Lock() if slow_log_path else None

    def schedule_delay(i: int) -> None:
        if mode == "stagger":
            time.sleep(i * stagger_delay_s)
        if max_start_rate_per_s and max_start_rate_per_s > 0:
            interval = 1.0 / max_start_rate_per_s
            with rate_lock:
                now = time.perf_counter()
                start_at = max(now, next_slot[0])
                next_slot[0] = start_at + interval
            sleep_for = start_at - time.perf_counter()
            if sleep_for > 0:
                time.sleep(sleep_for)

    def worker(i: int) -> RequestLog:
        schedule_delay(i)
        with httpx.Client() as client:
            return _one_request_sync(
                client,
                api_url,
                file_name,
                file_bytes,
                i,
                timeout_s,
                max_retries,
                auth_headers or None,
                params=query_params,
                verbose=verbose,
                slow_threshold_s=slow_threshold_s if slow_log_path else None,
                slow_log_path=slow_log_path,
                slow_log_lock=slow_log_lock,
            )

    if verbose:
        print(f"POST {api_url}")
        print(f"File: {file_name} ({len(file_bytes)} bytes)")
        print(f"Users: {num_users}  mode={mode}  async=False")
        if query_params:
            print(f"Query: {query_params}")
        if auth_headers:
            if "Authorization" in auth_headers:
                print("Auth: Bearer ***")
            if "X-API-Key" in auth_headers:
                print("Auth: X-API-Key ***")
        else:
            print(
                "Auth: (none) — expect 401 unless API has allow_anonymous_jobs enabled."
            )
        if max_start_rate_per_s:
            print(f"Start rate cap: {max_start_rate_per_s} req/s")
        if slow_log_path:
            print(f"Client slow log (>{slow_threshold_s}s): {slow_log_path}")
        print("-" * 52)

    with ThreadPoolExecutor(max_workers=num_users) as ex:
        futures = [ex.submit(worker, i) for i in range(num_users)]
        for fut in as_completed(futures):
            log = fut.result()
            summary.logs.append(log)
            summary.total += 1
            summary.all_elapsed_s.append(log.elapsed_s)
            if log.ok:
                summary.success += 1
                summary.response_times_s.append(log.elapsed_s)
            else:
                summary.failure += 1

    summary.total_wall_s = time.perf_counter() - wall0
    summary.logs.sort(key=lambda x: x.index)
    return summary


async def _one_request_async(
    client: httpx.AsyncClient,
    url: str,
    file_name: str,
    file_bytes: bytes,
    index: int,
    timeout: float,
    max_retries: int,
    headers: dict[str, str] | None = None,
    params: dict[str, str] | None = None,
    verbose: bool = True,
    slow_threshold_s: float | None = None,
    slow_log_path: Path | None = None,
    slow_log_lock: threading.Lock | None = None,
) -> RequestLog:
    t0 = time.perf_counter()
    last_status: int | None = None
    last_err: str | None = None
    files = {"file": (file_name, file_bytes, _mime_for_filename(file_name))}

    for attempt in range(max_retries + 1):
        try:
            r = await client.post(
                url,
                files=files,
                headers=headers,
                timeout=timeout,
                params=params or None,
            )
            elapsed = time.perf_counter() - t0
            ok = 200 <= r.status_code < 300
            last_status = r.status_code
            detail = _error_detail_from_response(r) if not ok else None
            _maybe_log_slow(
                path=slow_log_path,
                lock=slow_log_lock,
                threshold_s=slow_threshold_s,
                elapsed=elapsed,
                index=index,
                status=r.status_code,
                ok=ok,
            )

            if ok:
                if verbose:
                    print(
                        f"  [{index:4d}] status={r.status_code} "
                        f"time={elapsed:.3f}s attempt={attempt + 1}"
                    )
                return RequestLog(
                    index=index,
                    status_code=r.status_code,
                    elapsed_s=elapsed,
                    ok=True,
                    error=None,
                )

            last_err = detail or f"HTTP {r.status_code}"
            if attempt < max_retries and _should_retry(r.status_code, None):
                delay = RETRY_BASE_DELAY_S * (2**attempt)
                if verbose:
                    print(
                        f"  [{index:4d}] status={r.status_code} retry in {delay:.2f}s "
                        f"(attempt {attempt + 1}) | {detail}"
                    )
                await asyncio.sleep(delay)
                continue

            if verbose:
                print(
                    f"  [{index:4d}] status={r.status_code} "
                    f"time={elapsed:.3f}s (final) | {detail}"
                )
            return RequestLog(
                index=index,
                status_code=r.status_code,
                elapsed_s=elapsed,
                ok=False,
                error=last_err,
            )

        except Exception as e:
            last_err = str(e)
            elapsed = time.perf_counter() - t0
            _maybe_log_slow(
                path=slow_log_path,
                lock=slow_log_lock,
                threshold_s=slow_threshold_s,
                elapsed=elapsed,
                index=index,
                status=last_status,
                ok=False,
            )
            if attempt < max_retries and _should_retry(None, e):
                delay = RETRY_BASE_DELAY_S * (2**attempt)
                if verbose:
                    print(
                        f"  [{index:4d}] error={last_err!r} retry in {delay:.2f}s"
                    )
                await asyncio.sleep(delay)
                continue
            if verbose:
                print(
                    f"  [{index:4d}] FAILED time={elapsed:.3f}s err={last_err}"
                )
            return RequestLog(
                index=index,
                status_code=last_status,
                elapsed_s=elapsed,
                ok=False,
                error=last_err,
            )

    elapsed = time.perf_counter() - t0
    if verbose:
        print(
            f"  [{index:4d}] FAILED status={last_status} "
            f"time={elapsed:.3f}s err={last_err}"
        )
    return RequestLog(
        index=index,
        status_code=last_status,
        elapsed_s=elapsed,
        ok=False,
        error=last_err,
    )


async def _run_async(
    num_users: int,
    api_url: str,
    file_name: str,
    file_bytes: bytes,
    mode: TrafficMode,
    stagger_delay_s: float,
    max_start_rate_per_s: float | None,
    max_retries: int,
    timeout_s: float,
    auth_headers: dict[str, str],
    query_params: dict[str, str],
    verbose: bool,
    slow_threshold_s: float | None,
    slow_log_path: Path | None,
) -> RunSummary:
    summary = RunSummary(concurrent_users=num_users)
    wall0 = time.perf_counter()
    rate_lock = asyncio.Lock()
    next_slot: list[float] = [time.perf_counter()]
    slow_log_lock = threading.Lock() if slow_log_path else None

    async def launch_with_timing(i: int) -> RequestLog:
        if mode == "stagger":
            await asyncio.sleep(i * stagger_delay_s)
        if max_start_rate_per_s and max_start_rate_per_s > 0:
            interval = 1.0 / max_start_rate_per_s
            async with rate_lock:
                now = time.perf_counter()
                start_at = max(now, next_slot[0])
                next_slot[0] = start_at + interval
            sleep_for = start_at - time.perf_counter()
            if sleep_for > 0:
                await asyncio.sleep(sleep_for)

        async with httpx.AsyncClient() as client:
            return await _one_request_async(
                client,
                api_url,
                file_name,
                file_bytes,
                i,
                timeout_s,
                max_retries,
                auth_headers or None,
                params=query_params,
                verbose=verbose,
                slow_threshold_s=slow_threshold_s if slow_log_path else None,
                slow_log_path=slow_log_path,
                slow_log_lock=slow_log_lock,
            )

    if verbose:
        print(f"POST {api_url}")
        print(f"File: {file_name} ({len(file_bytes)} bytes)")
        print(f"Users: {num_users}  mode={mode}  async=True")
        if query_params:
            print(f"Query: {query_params}")
        if auth_headers:
            if "Authorization" in auth_headers:
                print("Auth: Bearer ***")
            if "X-API-Key" in auth_headers:
                print("Auth: X-API-Key ***")
        else:
            print(
                "Auth: (none) — expect 401 unless API has allow_anonymous_jobs enabled."
            )
        if max_start_rate_per_s:
            print(f"Start rate cap: {max_start_rate_per_s} req/s")
        if slow_log_path:
            print(f"Client slow log (>{slow_threshold_s}s): {slow_log_path}")
        print("-" * 52)

    tasks = [asyncio.create_task(launch_with_timing(i)) for i in range(num_users)]
    results = await asyncio.gather(*tasks)
    for log in results:
        summary.logs.append(log)
        summary.total += 1
        summary.all_elapsed_s.append(log.elapsed_s)
        if log.ok:
            summary.success += 1
            summary.response_times_s.append(log.elapsed_s)
        else:
            summary.failure += 1

    summary.total_wall_s = time.perf_counter() - wall0
    summary.logs.sort(key=lambda x: x.index)
    return summary


DEFAULT_PRESET_LEVELS = (100, 500, 1000)


def _default_users() -> int:
    raw = os.environ.get("LOAD_TEST_USERS", "10")
    try:
        return max(1, int(raw))
    except ValueError:
        return 10


def _default_api_key() -> str | None:
    return os.environ.get("LOAD_TEST_API_KEY") or os.environ.get("SEED_API_KEY")


def _query_params_from_export(export: str) -> dict[str, str]:
    e = (export or "").strip().lower()
    if not e:
        return {}
    return {"export": e}


def _parse_preset_list(raw: str) -> list[int]:
    out: list[int] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        out.append(max(1, int(part)))
    return out


def print_preset_comparison(summaries: list[RunSummary]) -> None:
    if len(summaries) < 2:
        return
    print()
    print("=" * 52)
    print("PRESET COMPARISON (successful requests)")
    print("=" * 52)
    hdr = f"{'Users':>8} {'p95(2xx)s':>12} {'p99(2xx)s':>12} {'err%':>8} {'thr(2xx)/s':>12}"
    print(hdr)
    print("-" * len(hdr))
    for s in summaries:
        ok = sorted(s.response_times_s) if s.response_times_s else []
        p95 = _percentile(ok, 95) if ok else 0.0
        p99 = _percentile(ok, 99) if ok else 0.0
        print(
            f"{s.concurrent_users:8d} {p95:12.3f} {p99:12.3f} "
            f"{s.error_rate() * 100:8.2f} {s.throughput_success_rps():12.2f}"
        )
    print("=" * 52)


def print_optimization_hints(summaries: list[RunSummary]) -> None:
    slow = []
    for s in summaries:
        if not s.response_times_s:
            continue
        ok = sorted(s.response_times_s)
        if _percentile(ok, 99) > 3.0:
            slow.append(s.concurrent_users)
    if not slow:
        return
    print()
    print("=" * 52)
    print(
        "OPTIMIZATION HINTS (p99 2xx latency > 3s for concurrency levels: "
        + ", ".join(str(x) for x in slow)
        + ")"
    )
    print("=" * 52)
    print(
        "  • Async: keep blocking work off the event loop (use asyncio.to_thread, "
        "RQ workers, connection pool tuning)."
    )
    print(
        "  • Queues: POST /translate enqueues jobs by default; scale RQ workers + Redis "
        "and tune rq_max_queue_depth / worker count."
    )
    print(
        "  • Caching: consider content-addressed cache (hash of file + export) where "
        "business rules allow; avoid duplicate OpenAI spend."
    )
    print(
        "  • Sync translate (ENABLE_SYNC_TRANSLATE): in-process pipeline — avoid for "
        "production load; profile PipelinePerfReport logs for hotspots."
    )
    print(
        "  • Infra: DB pool size, Redis latency, Supabase pooler limits, uvicorn "
        "workers vs async concurrency."
    )
    print("=" * 52)


def main() -> None:
    p = argparse.ArgumentParser(description="Load test POST /translate")
    p.add_argument(
        "--users",
        "-n",
        type=int,
        default=_default_users(),
        help="Concurrent users (default: LOAD_TEST_USERS or 10); ignored when --presets",
    )
    p.add_argument(
        "--presets",
        action="store_true",
        help="Run sequential tests for 100, 500, and 1000 concurrent users (see --preset-list)",
    )
    p.add_argument(
        "--preset-list",
        type=str,
        default=None,
        metavar="N,N,N",
        help=(
            "Comma-separated concurrency levels when using --presets "
            f"(default: {','.join(str(x) for x in DEFAULT_PRESET_LEVELS)})"
        ),
    )
    p.add_argument(
        "--url",
        default=API_URL,
        help="Full translate URL (default: LOAD_TEST_URL in .env or http://127.0.0.1:8000/translate)",
    )
    p.add_argument(
        "--file",
        "-f",
        type=Path,
        default=FILE_PATH,
        help="File to upload (default: LOAD_TEST_FILE or ./sample.pdf)",
    )
    p.add_argument(
        "--export",
        default="docx",
        help="Query param export=… (docx|pdf|both); empty string to omit",
    )
    p.add_argument(
        "--mode",
        choices=("burst", "stagger"),
        default="burst",
        help="burst=all at once; stagger=delay between starts",
    )
    p.add_argument(
        "--stagger",
        type=float,
        default=0.25,
        help="Seconds between staggered starts (mode=stagger)",
    )
    p.add_argument(
        "--max-rps",
        type=float,
        default=None,
        help="Max request *start* rate per second (optional throttle)",
    )
    p.add_argument("--retries", type=int, default=MAX_RETRIES)
    p.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_S)
    p.add_argument("--async", dest="use_async", action="store_true")
    p.add_argument("--quiet", action="store_true", help="Suppress per-request log lines")
    p.add_argument(
        "--slow-log",
        dest="slow_log",
        type=Path,
        default=None,
        help="Append client-side slow request lines (see --slow-threshold)",
    )
    p.add_argument(
        "--slow-threshold",
        type=float,
        default=3.0,
        help="Client slow-log threshold in seconds (default: 3)",
    )
    p.add_argument(
        "--json-report",
        dest="json_report",
        type=Path,
        default=None,
        help="Write machine-readable metrics (single run or preset series)",
    )
    p.add_argument(
        "--bearer",
        default=os.environ.get("LOAD_TEST_BEARER"),
        help="Supabase JWT (or LOAD_TEST_BEARER in .env). Prefix Bearer added if missing.",
    )
    p.add_argument(
        "--api-key",
        dest="api_key",
        default=_default_api_key(),
        help="X-API-Key or LOAD_TEST_API_KEY / SEED_API_KEY in .env if API allows key auth.",
    )
    args = p.parse_args()

    qp = _query_params_from_export(args.export)
    slow_path = args.slow_log.resolve() if args.slow_log else None
    if args.presets:
        if args.preset_list:
            levels = _parse_preset_list(args.preset_list)
        else:
            levels = list(DEFAULT_PRESET_LEVELS)
        if not levels:
            p.error("--preset-list produced no levels")
        summaries: list[RunSummary] = []
        for n in levels:
            print(f"\n>>> Scenario: {n} concurrent users")
            s = run_test(
                n,
                api_url=args.url,
                file_path=args.file,
                mode=args.mode,
                stagger_delay_s=args.stagger,
                max_start_rate_per_s=args.max_rps,
                max_retries=args.retries,
                timeout_s=args.timeout,
                use_async=args.use_async,
                bearer=args.bearer,
                api_key=args.api_key,
                query_params=qp,
                verbose=not args.quiet,
                slow_threshold_s=args.slow_threshold,
                slow_log_path=slow_path,
            )
            s.print_report(title=f"RESULT SUMMARY ({n} concurrent users)")
            summaries.append(s)
        print_preset_comparison(summaries)
        print_optimization_hints(summaries)
        if args.json_report:
            out = {
                "preset_levels": levels,
                "runs": [s.stats_dict() for s in summaries],
            }
            args.json_report.parent.mkdir(parents=True, exist_ok=True)
            args.json_report.write_text(json.dumps(out, indent=2), encoding="utf-8")
            print(f"\nWrote JSON report: {args.json_report.resolve()}")
        return

    s = run_test(
        args.users,
        api_url=args.url,
        file_path=args.file,
        mode=args.mode,
        stagger_delay_s=args.stagger,
        max_start_rate_per_s=args.max_rps,
        max_retries=args.retries,
        timeout_s=args.timeout,
        use_async=args.use_async,
        bearer=args.bearer,
        api_key=args.api_key,
        query_params=qp,
        verbose=not args.quiet,
        slow_threshold_s=args.slow_threshold,
        slow_log_path=slow_path,
    )
    s.print_report()
    print_optimization_hints([s])
    if args.json_report:
        args.json_report.parent.mkdir(parents=True, exist_ok=True)
        args.json_report.write_text(
            json.dumps({"runs": [s.stats_dict()]}, indent=2),
            encoding="utf-8",
        )
        print(f"\nWrote JSON report: {args.json_report.resolve()}")


if __name__ == "__main__":
    main()
