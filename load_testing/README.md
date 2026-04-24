# Load testing `/translate`

The harness lives at [`../load_test.py`](../load_test.py). It fires concurrent `POST` requests with a multipart file field (`file`), optional `export` query parameter, and the same auth headers the browser uses (`Authorization` or `X-API-Key`).

## Prerequisites

1. API running **without `--reload`** for realistic numbers (e.g. [`../run_api.ps1`](../run_api.ps1) or `uvicorn app.main:app --host 127.0.0.1 --port 8000`). On Linux/macOS you can add `--workers N` for multiple processes.
2. **Thread pool:** `POST /translate` uses `asyncio.to_thread` for auth + enqueue. The default executor size is controlled by **`ASYNCIO_THREAD_POOL_MAX_WORKERS`** (default **128**). Raise if you still see queueing under burst load.
3. A small sample file next to the script or pass `--file` (PDF, DOCX, or TXT).
4. Valid credentials: Supabase JWT (`--bearer`) and/or dev API key (`--api-key`), unless anonymous jobs are enabled.

## Rate limits and high concurrency

`POST /translate` is protected by SlowAPI (**300 requests per minute per** API key / Bearer hash / IP). Bursting **100–1000** requests from one client will return **429** unless you relax limits for the test environment.

For dedicated load-test or staging hosts only, set:

```text
HTTP_RATE_LIMIT_ENABLED=false
```

Never use this in production.

## Slow request logging (server)

The API logs a **WARNING** when any request takes at least `SLOW_REQUEST_LOG_THRESHOLD_S` seconds (default **3**). Set to **0** to disable.

```text
SLOW_REQUEST_LOG_THRESHOLD_S=3
```

Correlate with `X-Request-ID` in logs.

## Commands

**Single run** (default 10 users, override with `-n`):

```powershell
cd backend
python load_test.py -n 100 --api-key YOUR_KEY --url http://127.0.0.1:8000/translate
```

**Preset scenarios (100, 500, 1000 concurrent users)** — quiet output and JSON metrics:

```powershell
python load_test.py --presets --quiet --api-key YOUR_KEY --json-report load_metrics.json
```

**Custom preset list**:

```powershell
python load_test.py --presets --preset-list 100,250,500 --bearer YOUR_JWT
```

**Async HTTP client** (one event loop, many concurrent coroutines):

```powershell
python load_test.py --presets --async --api-key YOUR_KEY --quiet
```

**Client-side slow log** (lines for round-trips ≥ threshold):

```powershell
python load_test.py -n 500 --slow-log slow_clients.log --slow-threshold 3 --api-key YOUR_KEY --quiet
```

## Metrics reported

- **Latency (2xx):** average, p50, p95, p99, min, max  
- **Latency (all):** includes errors; useful when debugging 429/5xx  
- **Throughput:** successful requests per second and attempted requests per second over wall time  
- **Error rate**

## Observability recommendations

- **Structured logs:** ship uvicorn/app logs to your stack (Datadog, CloudWatch, Grafana Loki) and filter `Slow HTTP request`.
- **Traces:** OpenTelemetry for FastAPI + httpx client to OpenAI/Redis/Postgres; sample under load.
- **RED metrics:** request rate, error ratio, duration histograms for `/translate` and RQ job completion time.
- **Queues:** monitor Redis/RQ queue depth, worker count, and `rq_max_queue_depth` rejections.
- **Saturation:** DB pool usage, Redis latency, OpenAI rate limits (429), disk I/O on temp/job directories.

## Interpreting results

- **Default path:** `POST /translate` returns `{"job_id", "status": "pending"}` quickly; heavy work runs in **RQ workers**. If this JSON is slow, optimize enqueue (DB writes, validation, disk) and connection pools—not only the translation model.
- **Sync translate** (`ENABLE_SYNC_TRANSLATE=true`): measures full pipeline latency in-process; use for dev profiling, not as the production SLO.

When p99 exceeds **3s**, see the **OPTIMIZATION HINTS** section printed by the script and scale workers, tune pools, and add caching where product rules allow.
