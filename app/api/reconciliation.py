"""POST /api/reconciliation — 1С export ↔ foreman sheet diff."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status

from app.config import Settings, get_settings
from app.core.logging import get_logger
from app.deps import get_onec_parser_service, get_sheets_service
from app.pipelines.reconciliation import reconcile
from app.services.onec import OneCParserService
from app.services.sheets import SheetsService

log = get_logger("api.reconciliation")

router = APIRouter(tags=["reconciliation"])

_ALLOWED_EXTENSIONS = (".xls", ".xlsx", ".csv")
_ALLOWED_CONTENT_TYPES = {
    "application/vnd.ms-excel",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "text/csv",
    "application/csv",
    "application/octet-stream",
}


@router.post("/api/reconciliation", status_code=status.HTTP_200_OK)
async def reconcile_endpoint(
    file: Annotated[UploadFile, File()],
    onec: Annotated[OneCParserService, Depends(get_onec_parser_service)],
    sheets: Annotated[SheetsService, Depends(get_sheets_service)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, object]:
    """Accept the 1С export, run the reconciliation pipeline, return the diff."""
    correlation_id = uuid.uuid4().hex

    if not file.filename:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="filename is required",
        )

    name_lower = file.filename.lower()
    media_type = (file.content_type or "").lower()
    extension_ok = any(name_lower.endswith(ext) for ext in _ALLOWED_EXTENSIONS)
    if not extension_ok and media_type not in _ALLOWED_CONTENT_TYPES:
        log.warning(
            "reconciliation.unsupported_type",
            correlation_id=correlation_id,
            filename=file.filename,
            content_type=media_type,
        )
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail=(
                f"Unsupported file: {file.filename!r} "
                f"({media_type or 'unknown'}); expected .xls/.xlsx/.csv"
            ),
        )

    raw = await file.read()
    if len(raw) > settings.max_upload_bytes:
        log.warning(
            "reconciliation.too_large",
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
        log.warning(
            "reconciliation.empty",
            correlation_id=correlation_id,
            filename=file.filename,
        )
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Uploaded file is empty",
        )

    log.info(
        "reconciliation.accepted",
        correlation_id=correlation_id,
        filename=file.filename,
        content_type=media_type,
        bytes=len(raw),
    )

    result = await reconcile(
        raw=raw,
        filename=file.filename,
        onec=onec,
        sheets=sheets,
        correlation_id=correlation_id,
    )
    return result.model_dump(mode="json")
