"""FastAPI entrypoint: sync translate, async jobs, webhooks."""

from __future__ import annotations

import asyncio
import logging
import sys
import time
import uuid
from typing import Any
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager

# Windows: default asyncio loop uses select() (~512 sockets); Proactor scales further.
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from starlette.responses import Response
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

from app.api.routes import billing as billing_routes
from app.api.routes import document_pdf as document_pdf_routes
from app.api.routes import download as download_routes
from app.api.routes import translate as translate_routes
from app.api.routes import jobs as jobs_routes
from app.api.routes import webhooks as webhooks_routes
from app.api.routes import me as me_routes
from app.api.routes import referrals as referrals_routes
from app.api.routes import legacy_compat
from sqlalchemy.exc import IntegrityError, OperationalError

from app.core.pipeline_settings import get_pipeline_settings
from app.db.session import create_all_tables
from app.deps.auth import ensure_seed_user
from app.limiter import limiter
from app.services.razorpay_standard_checkout import (
    CreateOrderRequest,
    create_checkout_order,
    verify_checkout_payment,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    from app.core.pipeline_settings import get_pipeline_settings
    from app.services.job_cleanup import cleanup_expired_job_files

    settings = get_pipeline_settings()
    blocking_executor = ThreadPoolExecutor(
        max_workers=settings.asyncio_thread_pool_max_workers,
        thread_name_prefix="blocking",
    )
    asyncio.get_running_loop().set_default_executor(blocking_executor)

    settings.data_dir.mkdir(parents=True, exist_ok=True)
    try:
        # Run sync DB work in a thread so the event loop can still serve HTTP (e.g. /health)
        # while connecting; connect_timeout on the engine caps hang duration.
        await asyncio.to_thread(create_all_tables)
    except OperationalError as e:
        err = str(e.orig) if getattr(e, "orig", None) else str(e)
        hint = ""
        if "Tenant or user not found" in err:
            hint = (
                " Pooler username must be postgres.<project_ref> (from your *.supabase.co URL), "
                "not plain postgres. Copy the URI from Connect → Transaction pooler."
            )
        logger.error(
            "Database unreachable at startup (set SUPABASE_DATABASE_URL from "
            "Supabase → Connect → Transaction pooler; use IPv4 pooler host if db.* fails DNS).%s Error: %s",
            hint,
            e,
        )
    try:
        await asyncio.to_thread(ensure_seed_user)
    except OperationalError as e:
        logger.error("Seed user skipped (database unreachable): %s", e)
    except IntegrityError as e:
        logger.error("Seed user failed (database constraint): %s", e)

    try:
        from app.services.document_template_render import init_document_template_render
        from app.services.formatter.html_to_pdf_weasyprint import (
            warm_weasyprint_static_caches,
        )

        def _prewarm_doc_generation() -> None:
            init_document_template_render()
            warm_weasyprint_static_caches()

        await asyncio.to_thread(_prewarm_doc_generation)
    except Exception as e:
        if settings.is_production:
            from app.services.formatter.html_to_pdf_weasyprint import (
                WEASYPRINT_OSDEPS_INSTALL_HINT,
            )

            logger.error(
                "Document template prewarm failed: %s. %s",
                e,
                WEASYPRINT_OSDEPS_INSTALL_HINT,
            )
        else:
            logger.warning(
                "Document template prewarm failed (first PDF/HTML request may be slower): %s",
                e,
            )

    async def _cleanup_loop() -> None:
        while True:
            await asyncio.sleep(900)
            try:
                n = cleanup_expired_job_files()
                if n:
                    logger.info("Periodic cleanup removed %d job workspace(s)", n)
            except Exception:
                logger.exception("Job file cleanup failed")

    cleanup_task = asyncio.create_task(_cleanup_loop())
    if settings.is_production:
        if settings.seed_api_key:
            logger.warning(
                "SEED_API_KEY is set in production — remove or rotate after bootstrap; "
                "do not use dev keys on live traffic.",
            )
        if settings.supabase_allow_api_key_fallback:
            logger.warning(
                "SUPABASE_ALLOW_API_KEY_FALLBACK is true in production — set false "
                "if clients should use Supabase JWT only (reduces API-key abuse).",
            )
        if not settings.openai_api_key:
            logger.error(
                "OPENAI_API_KEY is not set; translation will fail until configured.",
            )
        dev_only_origins = frozenset(
            {
                "http://127.0.0.1:5173",
                "http://localhost:5173",
                "http://127.0.0.1:3000",
                "http://localhost:3000",
            },
        )
        cors = settings.cors_origins_list()
        if cors and set(cors).issubset(dev_only_origins):
            logger.warning(
                "CORS_ORIGINS is still dev-localhost only (%s). Browsers on "
                "https://www.<your-domain> calling a separate api.* host need production origins — "
                "set CORS_ORIGINS=https://www.porpin.com,https://porpin.com "
                "(comma-separated), restart workers, see backend/.env.example.",
                settings.cors_origins,
            )
    try:
        yield
    finally:
        cleanup_task.cancel()
        try:
            await cleanup_task
        except asyncio.CancelledError:
            pass
        blocking_executor.shutdown(wait=True)


def create_app() -> FastAPI:
    settings = get_pipeline_settings()
    app = FastAPI(
        title="Porpin",
        version="1.0.0",
        description="Upload PDF, DOCX, EPUB, or TXT; receive Hinglish DOCX / PDF / ZIP. Optional async jobs with Postgres + Redis.",
        lifespan=lifespan,
        openapi_tags=[
            {
                "name": "billing",
                "description": "Razorpay: **POST /api/create-order** and **POST /api/verify-payment** (standard checkout), pay-per-job PAYG, subscription, webhooks.",
            },
        ],
    )
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

    trusted = settings.trusted_hosts_list()
    if trusted:
        app.add_middleware(TrustedHostMiddleware, allowed_hosts=trusted)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins_list(),
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.middleware("http")
    async def security_headers_middleware(request: Request, call_next):
        response = await call_next(request)
        if settings.is_production:
            response.headers.setdefault("X-Content-Type-Options", "nosniff")
            response.headers.setdefault("X-Frame-Options", "DENY")
            response.headers.setdefault(
                "Referrer-Policy", "strict-origin-when-cross-origin"
            )
        return response

    @app.middleware("http")
    async def request_id_middleware(request: Request, call_next):
        rid = request.headers.get("x-request-id") or str(uuid.uuid4())
        request.state.request_id = rid
        response = await call_next(request)
        response.headers["X-Request-ID"] = rid
        return response

    @app.middleware("http")
    async def slow_request_logging_middleware(request: Request, call_next):
        t0 = time.perf_counter()
        response = await call_next(request)
        elapsed = time.perf_counter() - t0
        threshold = get_pipeline_settings().slow_request_log_threshold_s
        if threshold > 0 and elapsed >= threshold:
            rid = getattr(request.state, "request_id", None)
            logger.warning(
                "Slow HTTP request method=%s path=%s elapsed_s=%.3f request_id=%s",
                request.method,
                request.url.path,
                elapsed,
                rid,
            )
        return response

    app.include_router(legacy_compat.api_router)
    app.include_router(legacy_compat.outputs_router)
    app.include_router(legacy_compat.plain_router)
    app.include_router(me_routes.router)
    app.include_router(billing_routes.router)
    app.include_router(referrals_routes.router)
    app.include_router(translate_routes.router, tags=["translate"])
    app.include_router(document_pdf_routes.router)
    app.include_router(jobs_routes.router)
    app.include_router(download_routes.router)
    app.include_router(webhooks_routes.router, tags=["webhooks"])

    # Register on the FastAPI instance (not a separate APIRouter). Survives odd reload / import caching.
    @app.post("/api/create-order", tags=["billing"])
    def razorpay_checkout_create_order(body: CreateOrderRequest) -> dict[str, str | int]:
        """Razorpay standard web checkout: create order (amount in paise)."""
        return create_checkout_order(body)

    @app.post("/api/verify-payment", tags=["billing"])
    def razorpay_checkout_verify_payment(body: dict[str, Any]) -> dict[str, bool]:
        """Verify Checkout payment signature (no DB side effects)."""
        return verify_checkout_payment(body)

    _checkout_path = "/api/create-order"
    _has_checkout = any(
        getattr(route, "path", None) == _checkout_path
        and "POST" in (getattr(route, "methods", None) or set())
        for route in app.routes
    )
    if not _has_checkout:
        logger.error(
            "POST %s missing — billing checkout router failed to register. "
            "Save files, stop uvicorn (Ctrl+C), run from backend/: "
            "uvicorn app.main:app --reload --host 127.0.0.1 --port 8000",
            _checkout_path,
        )
    else:
        logger.info(
            "Razorpay standard checkout: POST /api/create-order, POST /api/verify-payment "
            "(listed under Swagger tag **billing**). No /api/create; use **create-order**.",
        )

    logger.info(
        "Serving with Python: %s (if this is not backend\\venv\\Scripts\\python.exe, stop other "
        "uvicorn/python on :8000 and start only the project venv).",
        sys.executable,
    )

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.head("/health")
    async def health_head() -> Response:
        """HEAD for load balancers / wait-on (GET-only routes return 405 to HEAD)."""
        return Response(status_code=200)

    return app


app = create_app()
