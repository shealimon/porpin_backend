"""Minimal settings for the simplified translation pipeline (no DB/Redis)."""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Self

from pydantic import AliasChoices, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_BACKEND_DIR = Path(__file__).resolve().parents[2]

# Public: same directory that contains ``backend/.env`` (use for manual file reads).
BACKEND_ROOT_DIR = _BACKEND_DIR


def _load_backend_dotenv() -> None:
    """Load ``backend/.env`` into ``os.environ`` before settings are built.

    Ensures RAZORPAY_* and other keys apply even when the OS has an empty value that
    would otherwise block pydantic-settings from using the file.
    """
    p = _BACKEND_DIR / ".env"
    if not p.is_file():
        return
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv(p, override=True, encoding="utf-8")


_load_backend_dotenv()


class PipelineSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=_BACKEND_DIR / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        # Empty OS env values (e.g. RAZORPAY_PLAN_ID=) should not override backend/.env
        env_ignore_empty=True,
    )

    openai_api_key: str | None = Field(default=None, description="OpenAI API key")
    openai_model: str = Field(default="gpt-4o-mini")
    translation_temperature: float = Field(
        default=0.0,
        ge=0.0,
        le=2.0,
        description=(
            "Temperature for translation completions. 0 is deterministic and typically slightly "
            "faster than 0.2 for MT-style outputs."
        ),
    )
    max_upload_bytes: int = Field(
        default=100 * 1024 * 1024,
        ge=0,
        description="Max upload size in bytes (default 100MB). Use 0 for no limit.",
    )
    temp_dir: Path = Field(default_factory=lambda: Path(tempfile.gettempdir()))
    chunk_min_tokens: int = Field(default=500)
    chunk_max_tokens: int = Field(
        default=5500,
        ge=200,
        description=(
            "Upper bound per segment before splitting; larger segments mean fewer API units "
            "and better multi-segment batching (keep below model context limits)."
        ),
    )
    translate_max_concurrency: int = Field(
        default=8,
        ge=1,
        le=256,
        description=(
            "Legacy cap for sync/thread-based translation helpers. "
            "The default pipeline uses asyncio batching (see translate_batch_max_concurrency)."
        ),
    )
    translate_api_batch_max_input_tokens: int = Field(
        default=16000,
        ge=1500,
        le=200_000,
        description=(
            "Target max input tokens (segments only) per multi-segment completion; "
            "prompt/instruction reserve is subtracted automatically."
        ),
    )
    translate_api_batch_prompt_reserve_tokens: int = Field(
        default=1400,
        ge=0,
        le=50_000,
        description="Tokens reserved from each batch budget for system instructions and delimiters.",
    )
    translate_api_batch_max_segments: int = Field(
        default=28,
        ge=8,
        le=300,
        validation_alias=AliasChoices("TRANSLATE_API_BATCH_MAX_SEGMENTS"),
        description=(
            "Hard cap on how many segments go into one multi-segment JSON completion. "
            "Large batches (35+) often get extra keys or malformed JSON from the model; "
            "24–32 is a reliable range."
        ),
    )
    translate_batch_max_concurrency: int = Field(
        default=12,
        ge=1,
        le=512,
        description=(
            "Concurrent multi-segment OpenAI HTTP calls per process (inline prepare + batch path). "
            "Lower if you see 429 / TPM errors on gpt-4o-mini."
        ),
    )
    translate_batch_stagger_ms: float = Field(
        default=55.0,
        ge=0.0,
        le=5000.0,
        description=(
            "Delay before each batch i starts (i × this many ms), capped in code, "
            "to spread load under OpenAI TPM limits. Set 0 to disable."
        ),
    )
    translate_openai_max_inflight: int = Field(
        default=6,
        ge=1,
        le=128,
        description=(
            "Max concurrent OpenAI chat.completions HTTP calls per batched translate run. "
            "Caps total parallelism when JSON batching falls back to many single-segment requests. "
            "Lower this (e.g. 2–3) if you see 429 / TPM errors on gpt-4o-mini."
        ),
    )
    translation_progress_pulse_interval_s: float = Field(
        default=3.0,
        ge=0.0,
        le=120.0,
        description=(
            "Publish smoothed translation progress every N seconds while OpenAI runs, "
            "so the progress bar moves during long single-batch calls. 0 disables."
        ),
    )
    translate_batch_response_json: bool = Field(
        default=True,
        description="Use response_format for batch calls (turn off if the model rejects it).",
    )
    translate_batch_use_structured_json_schema: bool = Field(
        default=True,
        validation_alias=AliasChoices("TRANSLATE_BATCH_USE_STRUCTURED_JSON_SCHEMA"),
        description=(
            "If true, multi-segment batch calls may use OpenAI Structured Outputs (json_schema); "
            "see translate_batch_structured_schema_min_segments. "
            "Disable entirely if the API returns 400 for the schema (e.g. very large n)."
        ),
    )
    translate_batch_structured_schema_min_segments: int = Field(
        default=28,
        ge=1,
        le=300,
        validation_alias=AliasChoices(
            "TRANSLATE_BATCH_STRUCTURED_SCHEMA_MIN_SEGMENTS",
        ),
        description=(
            "Use json_schema (Structured Outputs) only when the batch has at least this many segments. "
            "Smaller batches start with json_object — faster on short docs; full batches (~28) keep schema "
            "where parse failures were common. Set to 1 to always prefer schema when enabled."
        ),
    )
    translate_use_process_wide_slot_sem: bool = Field(
        default=False,
        description=(
            "If true, legacy translate_chunk uses a process-wide semaphore (caps nested thread pools). "
            "The batched asyncio path does not require this."
        ),
    )
    libreoffice_soffice_path: str | None = Field(
        default=None,
        description="Optional path to soffice for DOCX→PDF (e.g. LibreOffice).",
    )
    use_docx_inplace: bool = Field(
        default=True,
        description="For DOCX input, write translations back into the source file (keeps styles).",
    )
    docx_rebuild_from_structure: bool = Field(
        default=True,
        validation_alias=AliasChoices("DOCX_REBUILD_FROM_STRUCTURE"),
        description=(
            "When True (non-in-place rebuild only), emit DOCX from StructuredDocument via "
            "structured_docx_builder. Set false to use the legacy python-docx path from "
            "ClassifiedBlock list (rollback / A-B comparison)."
        ),
    )
    pdf_use_pdfplumber_for_tables: bool = Field(
        default=False,
        description=(
            "If true, run pdfplumber per PDF page for table regions and cells (slower on long PDFs, better tables). "
            "Default false for faster translation; set PDF_USE_PDFPLUMBER_FOR_TABLES=true when table fidelity matters."
        ),
        validation_alias=AliasChoices("PDF_USE_PDFPLUMBER_FOR_TABLES"),
    )
    # --- Async jobs / scale-out (optional) ---
    database_url: str | None = Field(
        default=None,
        validation_alias=AliasChoices("SUPABASE_DATABASE_URL", "DATABASE_URL"),
        description=(
            "Supabase PostgreSQL connection URI (psycopg2). Prefer SUPABASE_DATABASE_URL. "
            "Dashboard → Connect → Transaction pooler: "
            "postgresql+psycopg2://postgres.[ref]:[password]@aws-0-[region].pooler.supabase.com:6543/postgres?sslmode=require"
        ),
    )
    database_pool_size: int = Field(
        default=20,
        ge=1,
        le=100,
        validation_alias=AliasChoices("DB_POOL_SIZE", "DATABASE_POOL_SIZE"),
        description=(
            "SQLAlchemy pool size per process (API + each worker). Raise under many concurrent "
            "requests; sum across all uvicorn + RQ + prepare workers must fit Supabase pooler limits."
        ),
    )
    database_max_overflow: int = Field(
        default=60,
        ge=0,
        le=200,
        validation_alias=AliasChoices("DB_MAX_OVERFLOW", "DATABASE_MAX_OVERFLOW"),
        description="Extra connections beyond pool_size when burst load spikes (per process).",
    )
    database_pool_timeout: float = Field(
        default=30.0,
        ge=5.0,
        le=120.0,
        validation_alias=AliasChoices("DB_POOL_TIMEOUT", "DATABASE_POOL_TIMEOUT"),
        description="Seconds to wait for a pooled connection before error.",
    )
    asyncio_thread_pool_max_workers: int = Field(
        default=128,
        ge=16,
        le=512,
        validation_alias=AliasChoices("ASYNCIO_THREAD_POOL_MAX_WORKERS"),
        description=(
            "Default ThreadPoolExecutor size for asyncio.to_thread (enqueue, DB-heavy work). "
            "Raise under many concurrent POST /translate calls; each process has its own pool."
        ),
    )
    allow_anonymous_jobs: bool = Field(
        default=False,
        description=(
            "If true, POST /translate and job endpoints accept requests with no "
            "Authorization / X-API-Key and attribute work to anonymous_job_user_id (dev only)."
        ),
    )
    anonymous_job_user_id: str | None = Field(
        default=None,
        description="UUID string for placeholder Profile when allow_anonymous_jobs is true.",
    )
    redis_url: str | None = Field(
        default=None,
        description="Redis URL for RQ job queue and quota counters.",
    )
    redis_max_connections: int = Field(
        default=200,
        ge=16,
        le=4096,
        validation_alias=AliasChoices("REDIS_MAX_CONNECTIONS"),
        description=(
            "Max connections in the shared sync Redis pool (API + RQ enqueue + workers). "
            "Raise under hundreds of concurrent users; stay within Redis server maxclients."
        ),
    )
    openai_http_max_connections: int = Field(
        default=256,
        ge=32,
        le=2048,
        validation_alias=AliasChoices("OPENAI_HTTP_MAX_CONNECTIONS"),
        description=(
            "httpx max_connections per AsyncOpenAI client (chunk_worker should use one long-lived client). "
            "Should be >= translate_global_max_inflight in aggregate across processes."
        ),
    )
    jwt_secret: str = Field(
        default="change-me-in-production",
        description="HS256 secret for short-lived job download tokens.",
    )
    environment: str = Field(
        default="development",
        validation_alias=AliasChoices("ENV", "ENVIRONMENT"),
        description=(
            "Set to production (or prod) for stricter validation and security headers. "
            "Requires a non-default JWT_SECRET and ALLOW_ANONYMOUS_JOBS=false."
        ),
    )
    cors_origins: str = Field(
        default="http://127.0.0.1:5173,http://localhost:5173",
        validation_alias=AliasChoices("CORS_ORIGINS"),
        description=(
            "Comma-separated CORS origins. Default is local Vite only; set CORS_ORIGINS on the "
            "production server (e.g. https://www.porpin.com,https://porpin.com)."
        ),
    )
    trusted_hosts: str | None = Field(
        default=None,
        validation_alias=AliasChoices("TRUSTED_HOSTS"),
        description=(
            "Comma-separated Host header values for TrustedHostMiddleware (e.g. api.example.com). "
            "Unset = middleware disabled. Include 127.0.0.1 if you probe health locally."
        ),
    )
    supabase_url: str | None = Field(
        default=None,
        description=(
            "Supabase project URL (Dashboard → Project Settings → API), e.g. "
            "https://<project-ref>.supabase.co. Optional; data lives in Postgres via SUPABASE_DATABASE_URL."
        ),
    )
    supabase_jwt_secret: str | None = Field(
        default=None,
        description="Supabase JWT secret (same as Project Settings → API → JWT Secret).",
    )
    supabase_allow_api_key_fallback: bool = Field(
        default=True,
        description="If false, only Bearer JWT is accepted (no X-API-Key).",
    )
    enable_sync_translate: bool = Field(
        default=False,
        description="If true, POST /translate may return file directly (legacy).",
    )
    http_rate_limit_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("HTTP_RATE_LIMIT_ENABLED"),
        description=(
            "If false, SlowAPI limits on /translate are skipped (use only in trusted "
            "load-test or staging environments)."
        ),
    )
    slow_request_log_threshold_s: float = Field(
        default=3.0,
        ge=0.0,
        validation_alias=AliasChoices("SLOW_REQUEST_LOG_THRESHOLD_S"),
        description=(
            "Log WARNING when any HTTP request takes at least this many seconds; "
            "0 disables slow-request logging."
        ),
    )
    http_translate_slowapi_limit: str = Field(
        default="300/minute",
        validation_alias=AliasChoices(
            "HTTP_TRANSLATE_RATE_LIMIT",
            "TRANSLATE_HTTP_SLOWAPI_LIMIT",
        ),
        description=(
            "SlowAPI limit for POST /translate (e.g. 300/minute, 2000/minute). "
            "Ignored when http_rate_limit_enabled is false."
        ),
    )
    cleanup_ttl_minutes: int = Field(
        default=45,
        ge=15,
        le=120,
        description="Delete job working files older than this (periodic cleanup).",
    )
    free_tier_words_per_month: int = Field(
        default=10_000,
        description="Approximate free tier word allowance (download gate heuristic).",
    )
    minimum_charge_inr: float = Field(default=5.0)
    rate_inr_per_10000_words: float = Field(
        default=9.9,
        description="Marginal INR per 10k words for marketing/API; keep = PAYG_INR_PER_100K_WORDS/10 from payg_pricing.",
    )
    payg_checkout_required: bool = Field(
        default=False,
        validation_alias=AliasChoices(
            "PAYG_CHECKOUT_REQUIRED",
            "PAYG_WALLET_REQUIRED",
        ),
        description=(
            "Pay-as-you-go (PayG): if True, Razorpay checkout is required per job before translation runs. "
            "If False, jobs can start without checkout (dev). "
            "Env alias PAYG_WALLET_REQUIRED is deprecated but still accepted."
        ),
    )
    seed_api_key: str | None = Field(
        default=None,
        description="If set, ensures a dev user exists with this raw API key (hash stored).",
    )
    seed_api_key_user_id: str | None = Field(
        default=None,
        validation_alias=AliasChoices("SEED_API_KEY_USER_ID"),
        description=(
            "UUID of an existing auth.users id: set so public.users.id matches Supabase Auth; "
            "otherwise profiles rows (FK auth.users) cannot be created for the seed API key."
        ),
    )
    stripe_webhook_secret: str | None = Field(
        default=None,
        description="Stripe signing secret for /webhooks/stripe (optional).",
    )
    razorpay_key_id: str | None = Field(
        default=None,
        validation_alias=AliasChoices("RAZORPAY_KEY_ID"),
    )
    razorpay_key_secret: str | None = Field(
        default=None,
        validation_alias=AliasChoices("RAZORPAY_KEY_SECRET"),
    )
    razorpay_webhook_secret: str | None = Field(
        default=None,
        validation_alias=AliasChoices("RAZORPAY_WEBHOOK_SECRET"),
    )
    razorpay_plan_id: str | None = Field(
        default=None,
        description="Razorpay subscription plan_id (e.g. plan_xx) for monthly billing.",
        validation_alias=AliasChoices("RAZORPAY_PLAN_ID"),
    )
    razorpay_yearly_plan_id: str | None = Field(
        default=None,
        description="Razorpay plan_id for yearly billing (2M words/month, bucket resets every 30 days).",
        validation_alias=AliasChoices("RAZORPAY_YEARLY_PLAN_ID"),
    )
    rq_queue_name: str = Field(default="translate")
    rq_high_priority_queue_name: str = Field(
        default="translate_high",
        description="Paid / priority tier (processed first when worker listens to both).",
    )
    rq_max_queue_depth: int = Field(
        default=10_000,
        ge=10,
        description=(
            "Reject new enqueue when default+high RQ depth exceeds this (protects overload). "
            "Raise for flash crowds (e.g. 500 concurrent uploads); scale RQ workers to match."
        ),
    )
    min_translate_tokens: int = Field(
        default=12,
        ge=0,
        description="Skip GPT for chunks smaller than this (token estimate).",
    )
    translate_api_word_ratio_max: float = Field(
        default=1.0,
        ge=0.5,
        le=1.0,
        description=(
            "At most this fraction of paragraph words are sent to the translation API; "
            "the rest stay verbatim. Use 1.0 for full Hinglish (recommended for books). "
            "Lower values (e.g. 0.78) skip additional sentences to save cost on reference-heavy text."
        ),
    )
    max_tokens_per_job: int = Field(
        default=400_000,
        ge=1000,
        description="Hard cap on estimated input+output tokens per job (cost guardrail).",
    )
    gpt_max_retries: int = Field(
        default=6,
        ge=1,
        le=8,
        description="Retries per chunk on rate limits / timeouts.",
    )
    translation_max_processing_attempts: int = Field(
        default=3,
        ge=1,
        le=10,
        description=(
            "On translation pipeline failure, re-enqueue the same job at most this many times "
            "(payment stays settled; user can also retry a failed job without paying again)."
        ),
    )
    data_dir: Path = Field(default_factory=lambda: _BACKEND_DIR / "data")
    free_tier_daily_jobs: int = Field(default=20)
    paid_tier_daily_jobs: int = Field(default=2000)
    translate_rate_limit: str = Field(default="60/minute")
    referral_referee_signup_bonus_words: int = Field(
        default=10_000,
        ge=0,
        validation_alias=AliasChoices("REFERRAL_REFEREE_SIGNUP_BONUS_WORDS"),
        description="Extra translation words granted to a new user when they sign up with a valid referral link.",
    )
    referral_referrer_verify_reward_words: int = Field(
        default=3_000,
        ge=0,
        validation_alias=AliasChoices("REFERRAL_REFERRER_VERIFY_REWARD_WORDS"),
        description=(
            "Words credited to the referrer when the referee verifies email and signs in (step 1)."
        ),
    )
    referral_referrer_first_payment_reward_words: int = Field(
        default=7_000,
        ge=0,
        validation_alias=AliasChoices(
            "REFERRAL_REFERRER_FIRST_PAYMENT_REWARD_WORDS",
            "REFERRAL_REFERRER_COMPLETION_REWARD_WORDS",
            "REFERRAL_WORDS_PER_SIGNUP",
        ),
        description=(
            "Additional words for the referrer after the referee's first payment: "
            "pay-as-you-go or monthly/yearly subscription (step 2)."
        ),
    )
    referral_max_rewarded_referrals: int = Field(
        default=10,
        ge=0,
        validation_alias=AliasChoices("REFERRAL_MAX_REWARDED_REFERRALS"),
        description=(
            "Maximum number of friends for whom the referrer can earn referral word credits "
            "(first N friends that receive any step-1 or step-2 payout; extra invites do not add credits)."
        ),
    )
    referral_max_words_earned_per_referrer: int = Field(
        default=100_000,
        ge=0,
        validation_alias=AliasChoices("REFERRAL_MAX_WORDS_EARNED_PER_REFERRER"),
        description=(
            "Lifetime ceiling on total referral reward words per referrer "
            "(e.g. 10 friends × 10k = 100k). Hits before the friend-count cap if rewards per friend rise."
        ),
    )
    use_sharded_chunk_queue: bool = Field(
        default=True,
        description=(
            "Use Redis chunk queue + prepare/finalize RQ jobs. Requires "
            "``python -m app.workers.chunk_worker`` (can run multiple processes). "
            "If false, falls back to monolithic process_document_job translation."
        ),
    )
    chunk_job_min_words: int = Field(
        default=3500,
        ge=0,
        le=80_000,
        description=(
            "Target minimum words per chunk-queue batch when "
            "``chunk_queue_pack_by_tokens`` is false (legacy word packing)."
        ),
    )
    chunk_job_max_words: int = Field(
        default=12_000,
        ge=100,
        le=200_000,
        description=(
            "Maximum words per chunk-queue batch when ``chunk_queue_pack_by_tokens`` is false."
        ),
    )
    chunk_queue_pack_by_tokens: bool = Field(
        default=True,
        description=(
            "If true, pack chunk-queue batches by tiktoken estimate (see chunk_queue_*_tokens). "
            "If false, use word-based packing (chunk_job_*_words)."
        ),
    )
    chunk_queue_min_tokens: int = Field(
        default=300,
        ge=50,
        le=20_000,
        description="Target minimum input tokens per chunk-queue batch (merge small tails).",
    )
    chunk_queue_max_tokens: int = Field(
        default=2200,
        ge=200,
        le=50_000,
        validation_alias=AliasChoices("CHUNK_QUEUE_MAX_TOKENS"),
        description=(
            "Maximum input tokens per chunk-queue batch. Higher = fewer API round-trips (faster on "
            "50k+ word books) and less ‘stuck’ time near the end; lower = more parallel chunks, "
            "smaller each request. Keep within the model context with prompt + completion overhead."
        ),
    )
    translation_cache_enabled: bool = Field(
        default=True,
        description=(
            "If true and REDIS_URL is set, cache segment translations in Redis (SHA-256 key) "
            "to skip duplicate OpenAI calls across jobs."
        ),
    )
    translation_cache_ttl_seconds: int = Field(
        default=86400 * 7,
        ge=60,
        le=86400 * 365,
        description="TTL for cached translation entries.",
    )
    rq_job_retry_max: int = Field(
        default=5,
        ge=0,
        le=15,
        description=(
            "RQ-level retries for prepare/finalize/monolithic jobs (transient worker/Redis failures). "
            "0 disables RQ retries (chunk-level retries still apply)."
        ),
    )
    translate_global_max_inflight: int = Field(
        default=256,
        ge=1,
        le=8000,
        description=(
            "Global Redis-backed cap on concurrent OpenAI chat.completions calls (all chunk_workers + inline prepare). "
            "Raise with your OpenAI TPM/RPM tier; lower if you see 429s."
        ),
    )
    chunk_queue_max_messages: int = Field(
        default=100_000,
        ge=0,
        description="Reject new job enqueue when Redis chunk queue length exceeds this (0 = disabled).",
    )
    chunk_worker_parallel_handlers: int = Field(
        default=12,
        ge=1,
        le=512,
        description="Concurrent chunk handlers per chunk_worker process (each respects global inflight).",
    )
    chunk_inflight_spin_seconds: float = Field(
        default=0.05,
        ge=0.01,
        le=2.0,
        description="Delay between retries when waiting for a global OpenAI inflight slot.",
    )
    chunk_task_max_retries: int = Field(
        default=3,
        ge=0,
        le=15,
        description="Chunk requeues after failure before marking the batch failed.",
    )
    inline_translation_max_batches: int = Field(
        default=0,
        ge=0,
        le=500,
        description=(
            "If >0 and batch count ≤ this, translate inside the RQ prepare job (parallel asyncio). "
            "Default 0 = always use the Redis chunk queue so chunk_worker processes scale horizontally."
        ),
    )
    translation_preview_max_pages: int = Field(
        default=3,
        ge=1,
        le=20,
        description="Translate this many leading pages (PDF) or word-budget equivalent for free preview.",
    )
    translation_preview_max_starts_per_day: int = Field(
        default=5,
        ge=1,
        le=100,
        description="Per signed-in user, max free preview starts per UTC day (abuse guard).",
    )
    translation_preview_words_per_page_estimate: int = Field(
        default=260,
        ge=50,
        le=800,
        description="For DOCX/TXT preview slicing: words per estimated page.",
    )
    translation_preview_max_words: int = Field(
        default=450,
        ge=100,
        le=20_000,
        description=(
            "Hard cap on words parsed/translated for free preview (keeps latency low). "
            "Effective budget is min(preview_pages × words_per_page_estimate, this value)."
        ),
    )

    @model_validator(mode="after")
    def _validate_production_safety(self) -> Self:
        env = (self.environment or "").strip().lower()
        if env in ("production", "prod"):
            if self.jwt_secret == "change-me-in-production":
                raise ValueError(
                    "In production, JWT_SECRET must be set to a long random value "
                    "(not the default placeholder)."
                )
            if self.allow_anonymous_jobs:
                raise ValueError(
                    "In production, ALLOW_ANONYMOUS_JOBS must be false so unauthenticated "
                    "users cannot enqueue translation jobs."
                )
        return self

    @property
    def is_production(self) -> bool:
        return (self.environment or "").strip().lower() in ("production", "prod")

    def cors_origins_list(self) -> list[str]:
        return [p.strip() for p in self.cors_origins.split(",") if p.strip()]

    def trusted_hosts_list(self) -> list[str] | None:
        if self.trusted_hosts is None:
            return None
        hosts = [p.strip() for p in self.trusted_hosts.split(",") if p.strip()]
        return hosts or None


def get_pipeline_settings() -> PipelineSettings:
    """Build settings on each call so updates to ``backend/.env`` apply without stale ``lru_cache``."""
    return PipelineSettings()
