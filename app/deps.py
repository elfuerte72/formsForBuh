"""FastAPI dependencies: auth + service factories."""

from __future__ import annotations

import secrets
from functools import lru_cache

import httpx
from anthropic import AsyncAnthropic
from fastapi import Depends, Header, HTTPException, status

from app.config import Settings, get_settings
from app.services.files import FilesService
from app.services.vision import VisionService


# --- Auth -------------------------------------------------------------------


async def verify_webhook_secret(
    x_webhook_secret: str | None = Header(default=None, alias="X-Webhook-Secret"),
    settings: Settings = Depends(get_settings),
) -> None:
    """Timing-safe check of the shared secret.

    Yandex Forms attaches the header configured in the integration UI.
    """
    expected = settings.webhook_secret
    if not x_webhook_secret or not secrets.compare_digest(x_webhook_secret, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing X-Webhook-Secret",
        )


# --- Singletons (cached) ----------------------------------------------------


@lru_cache(maxsize=1)
def get_anthropic_client() -> AsyncAnthropic:
    settings = get_settings()
    return AsyncAnthropic(api_key=settings.anthropic_api_key)


@lru_cache(maxsize=1)
def get_httpx_client() -> httpx.AsyncClient:
    settings = get_settings()
    return httpx.AsyncClient(
        timeout=settings.http_timeout_seconds,
        follow_redirects=True,
    )


# --- Service factories ------------------------------------------------------


def get_files_service(
    settings: Settings = Depends(get_settings),
) -> FilesService:
    return FilesService(
        client=get_httpx_client(),
        max_bytes=settings.max_download_bytes,
        dpi=settings.max_image_dpi,
    )


def get_vision_service(
    settings: Settings = Depends(get_settings),
) -> VisionService:
    return VisionService(
        client=get_anthropic_client(),
        model=settings.anthropic_model,
    )
