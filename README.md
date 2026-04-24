# Hinglish document translator (backend)

FastAPI service: structured parse → section skip → GPT Hinglish → DOCX/PDF/ZIP.

## Quick start

```bash
cd backend
python -m venv venv
.\venv\Scripts\activate   # Windows
pip install -r requirements.txt
copy .env.example .env    # set OPENAI_API_KEY
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

- **Sync translate:** `POST /translate` — form field `file`; query `export=docx|pdf|both` (ZIP needs PDF tooling on the server).
- **Health:** `GET /health`
- **Docs:** `GET /docs`

### Environment

| Variable | Purpose |
|----------|---------|
| `OPENAI_API_KEY` | Required for GPT calls |
| `LIBREOFFICE_SOFFICE_PATH` | Optional path to `soffice` for PDF/ZIP |
| `DATABASE_URL` | Optional — enables async **jobs** API |
| `REDIS_URL` | Optional — job queue (RQ) |
| `JWT_SECRET` | Secret for short-lived job download tokens |
| `SEED_API_KEY` | Optional dev API key (creates a **free** tier user in DB) |

### Async jobs (scale-out)

When `DATABASE_URL` and `REDIS_URL` are set:

1. Tables are created on startup (`users`, `translation_jobs`).
2. `POST /jobs` with header `X-API-Key` enqueues work; poll `GET /jobs/{id}`; download via URL returned when `completed`.
3. Run a worker: `python -m app.workers.redis_worker`

Use Postgres in production; SQLite is fine for local dev (`sqlite:///./data/app.db`).

### PDF / ZIP

`export=pdf` or `export=both` need **LibreOffice** (`soffice` on `PATH` or `LIBREOFFICE_SOFFICE_PATH`) or **docx2pdf** on Windows.

### Stripe

`POST /webhooks/stripe` accepts events (verify `STRIPE_WEBHOOK_SECRET` in production); extend handler to mark users **paid** in Postgres.
