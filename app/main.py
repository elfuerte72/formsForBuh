"""FastAPI application entry-point."""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api import upd_upload
from app.config import get_settings, redact_settings
from app.core.logging import configure_logging, get_logger
from app.deps import get_anthropic_client, get_httpx_client

log = get_logger("main")


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    configure_logging(level=settings.log_level, fmt=settings.log_format)
    log.info("app.starting", settings=redact_settings(settings))
    # Warm cached singletons so first request doesn't pay construction cost.
    get_anthropic_client()
    get_httpx_client()

    routes = [
        {"path": r.path, "methods": sorted(r.methods or [])}
        for r in app.routes
        if hasattr(r, "methods")
    ]
    log.info("app.started", routes=routes, log_level=settings.log_level)
    try:
        yield
    finally:
        log.info("app.stopping")
        client = get_httpx_client()
        await client.aclose()
        log.info("app.stopped")


app = FastAPI(
    title="formsForBuh — UPD upload webhook",
    version="0.1.0",
    lifespan=lifespan,
)

app.include_router(upd_upload.router)


@app.get("/health", tags=["infra"])
async def health() -> dict[str, str]:
    return {"status": "ok"}
