# API server only. Run RQ workers separately: e.g. rq worker -u $REDIS_URL translate translate_high
# Add LibreOffice in a derived image if you need DOCX→PDF on this host.

FROM python:3.12-slim-bookworm

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY alembic ./alembic
COPY alembic.ini .

EXPOSE 8000

# --proxy-headers: correct client IP / scheme behind nginx, Cloudflare, etc.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
