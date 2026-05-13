"""POST /api/upload — entry point for the custom UPD upload form."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status

from app.config import Settings, get_settings
from app.core.logging import get_logger
from app.deps import get_files_service, get_sheets_service, get_vision_service
from app.models import Foreman, UploadResult
from app.pipelines.upd_upload import process_upd
from app.services.files import FilesService
from app.services.sheets import SheetsService
from app.services.vision import VisionService

log = get_logger("api.upd_upload")

router = APIRouter(tags=["upd"])

_ALLOWED_CONTENT_TYPES = {
    "application/pdf",
    "image/png",
    "image/jpeg",
    "image/jpg",
    "image/webp",
}


@router.post("/api/upload", status_code=status.HTTP_200_OK)
async def upload_upd(
    foreman: Annotated[Foreman, Form(min_length=1, max_length=100)],
    file: Annotated[UploadFile, File()],
    files: Annotated[FilesService, Depends(get_files_service)],
    vision: Annotated[VisionService, Depends(get_vision_service)],
    sheets: Annotated[SheetsService, Depends(get_sheets_service)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, object]:
    """Accept a multipart upload, run the pipeline synchronously, return result."""
    correlation_id = uuid.uuid4().hex

    foreman = foreman.strip()
    if not foreman:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="foreman is required",
        )

    if not file.filename:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="filename is required",
        )

    media_type = (file.content_type or "").lower()
    if media_type not in _ALLOWED_CONTENT_TYPES:
        log.warning(
            "upload.unsupported_type",
            correlation_id=correlation_id,
            filename=file.filename,
            content_type=media_type,
        )
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail=f"Unsupported content type: {media_type or 'unknown'}",
        )

    raw = await file.read()
    if len(raw) > settings.max_upload_bytes:
        log.warning(
            "upload.too_large",
            correlation_id=correlation_id,
            filename=file.filename,
            bytes=len(raw),
            limit=settings.max_upload_bytes,
        )
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"File too large: {len(raw)} > {settings.max_upload_bytes}",
        )
    if not raw:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Uploaded file is empty",
        )

    log.info(
        "upload.accepted",
        correlation_id=correlation_id,
        foreman=foreman,
        filename=file.filename,
        content_type=media_type,
        bytes=len(raw),
    )

    result: UploadResult = await process_upd(
        raw=raw,
        filename=file.filename,
        media_type=media_type,
        foreman=foreman,
        files=files,
        vision=vision,
        sheets=sheets,
        settings=settings,
        correlation_id=correlation_id,
    )
    return result.model_dump(mode="json")
