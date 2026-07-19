# syntax=docker/dockerfile:1

# Builder stage: resolve and install only the production workspace packages.
# The PayPal Sandbox harness and its dependencies (Playwright, pytest, etc.)
# are intentionally not copied or installed.
FROM python:3.13-slim AS builder

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    UV_NO_CACHE=1 \
    UV_LINK_MODE=copy

RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir uv

COPY pyproject.toml uv.lock ./
COPY packages/payment-fee/pyproject.toml packages/payment-fee/README.md ./packages/payment-fee/
COPY packages/payment-fee/src ./packages/payment-fee/src
COPY services/payment-fee-service/pyproject.toml services/payment-fee-service/README.md ./services/payment-fee-service/
COPY services/payment-fee-service/src ./services/payment-fee-service/src

RUN uv sync --no-dev --frozen --package payment-fee-service --no-editable

# Runtime stage: only the virtual environment is copied; no source checkout
# or dev tools remain in the final image.
FROM python:3.13-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

COPY --from=builder /app/.venv /app/.venv

USER 65532:65532

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
  CMD /app/.venv/bin/python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health/live', timeout=2)"

CMD ["/app/.venv/bin/payment-fee-service", "serve", "--host", "0.0.0.0", "--port", "8000"]
