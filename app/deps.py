"""FastAPI dependencies: cached SDK clients + per-request service factories."""

from __future__ import annotations

from functools import lru_cache

import httpx
from anthropic import AsyncAnthropic
from fastapi import Depends

from app.config import Settings, get_settings
from app.services.files import FilesService
from app.services.sheets import SheetsService
from app.services.vision import VisionService


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


@lru_cache(maxsize=1)
def get_sheets_service() -> SheetsService:
    settings = get_settings()
    return SheetsService(
        credentials_json=settings.google_credentials_json,
        sheet_id=settings.sheet_id,
    )


# --- Service factories ------------------------------------------------------


def get_files_service(
    settings: Settings = Depends(get_settings),
) -> FilesService:
    return FilesService(
        client=get_httpx_client(),
        max_bytes=settings.max_upload_bytes,
        dpi=settings.max_image_dpi,
    )


def get_vision_service(
    settings: Settings = Depends(get_settings),
) -> VisionService:
    return VisionService(
        client=get_anthropic_client(),
        model=settings.anthropic_model,
    )
