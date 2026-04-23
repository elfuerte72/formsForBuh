# ---------- builder: install deps into .venv with uv ----------
FROM python:3.12-slim AS builder

# Copy uv binary from the official Astral image (pin a stable tag).
COPY --from=ghcr.io/astral-sh/uv:0.10.6 /uv /uvx /usr/local/bin/

ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PYTHON_DOWNLOADS=never \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# Cache-friendly: deps layer invalidates only when lock files change.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,id=uv-cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

# ---------- runtime: minimal image with only venv + source ----------
FROM python:3.12-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/app/.venv/bin:$PATH"

# Non-root user (must exist before the COPY --chown below).
RUN groupadd --system --gid 1001 app \
 && useradd  --system --uid 1001 --gid app --home-dir /app --shell /usr/sbin/nologin app

WORKDIR /app

# Venv built in the previous stage.
COPY --from=builder --chown=app:app /app/.venv /app/.venv

# Application source.
COPY --chown=app:app app ./app

USER app

# Default port for local `docker run`; Railway injects $PORT at runtime.
EXPOSE 8000

# Local default: IPv4 bind (works with `docker run -p 8000:8000`).
# Railway overrides startCommand in railway.toml with `--host ::` for IPv6 ingress.
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
