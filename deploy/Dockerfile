# Agent Broker — slim production Dockerfile
# Single-stage Python 3.11 slim build optimized for free-tier hosts (Koyeb 256MB, Render 512MB).
#
# Playwright + chromium is intentionally NOT installed: ~500MB image bloat for a
# fallback channel almost no agent invokes early. To enable browser-automation
# fallback later, switch `PLAYWRIGHT_INSTALL=true` build arg.

FROM python:3.11-slim AS runtime

ARG PLAYWRIGHT_INSTALL=false

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PORT=8000 \
    ENVIRONMENT=production \
    LOG_LEVEL=INFO

WORKDIR /app

# System deps — keep minimal. libpq for asyncpg if/when Postgres wired.
RUN apt-get update && apt-get install -y --no-install-recommends \
        gcc \
        libpq-dev \
        curl \
    && rm -rf /var/lib/apt/lists/*

# Install Python deps first (cached layer)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Optional Playwright (only if explicitly enabled)
RUN if [ "$PLAYWRIGHT_INSTALL" = "true" ]; then \
        playwright install chromium --with-deps; \
    else \
        echo "Skipping Playwright install — set PLAYWRIGHT_INSTALL=true at build time to enable."; \
    fi

# Copy app source
COPY . .

# Drop privileges
RUN useradd -m -u 1000 appuser && chown -R appuser:appuser /app
USER appuser

RUN mkdir -p reports

EXPOSE 8000

# Health check uses the /health endpoint; no httpx import to avoid extra cold-start cost
HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD curl -fsS http://localhost:${PORT}/health || exit 1

# Honor $PORT env (Koyeb / Render / Railway / Fly all set it)
CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000} --workers 1"]
