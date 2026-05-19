"""POST /api/upload — entry point for the custom UPD upload form."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status

from app.config import Settings, get_settings
from app.core.logging import get_logger
from app.deps import get_files_service, get_sheets_service, get_vision_service
from app.models import BatchUploadResult, Foreman, UploadResult
from app.pipelines.upd_upload import process_upd_batch
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
    files: Annotated[list[UploadFile], File()],
    files_svc: Annotated[FilesService, Depends(get_files_service)],
    vision: Annotated[VisionService, Depends(get_vision_service)],
    sheets: Annotated[SheetsService, Depends(get_sheets_service)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, object]:
    """Accept N files in one multipart request, return a :class:`BatchUploadResult`.

    Per-file validation failures are turned into ``UploadResult(ok=False, error=...)``
    so the rest of the batch is still processed. Request-level problems (no foreman,
    no files, too many files) still raise HTTP errors.
    """
    correlation_id = uuid.uuid4().hex

    foreman = foreman.strip()
    if not foreman:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="foreman is required",
        )
    if not files:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="at least one file is required",
        )
    if len(files) > settings.max_batch_files:
        log.warning(
            "upload.too_many_files",
            correlation_id=correlation_id,
            count=len(files),
            limit=settings.max_batch_files,
        )
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"Too many files: {len(files)} > {settings.max_batch_files}",
        )

    # Slot-by-slot result list keeps the original order; we fill the pipeline
    # output back into the slots where validation passed.
    slots: list[UploadResult | None] = [None] * len(files)
    valid: list[tuple[int, bytes, str, str]] = []  # (slot_index, raw, filename, media_type)

    for index, file in enumerate(files):
        filename = file.filename or f"file-{index}"
        media_type = (file.content_type or "").lower()
        if media_type not in _ALLOWED_CONTENT_TYPES:
            log.warning(
                "upload.unsupported_type",
                correlation_id=correlation_id,
                filename=filename,
                content_type=media_type,
            )
            slots[index] = UploadResult(
                ok=False,
                correlation_id=f"{correlation_id}-{index}",
                filename=filename,
                error="unsupported_file_type",
            )
            continue

        raw = await file.read()
        if len(raw) > settings.max_upload_bytes:
            log.warning(
                "upload.too_large",
                correlation_id=correlation_id,
                filename=filename,
                bytes=len(raw),
                limit=settings.max_upload_bytes,
            )
            slots[index] = UploadResult(
                ok=False,
                correlation_id=f"{correlation_id}-{index}",
                filename=filename,
                error="file_too_large",
            )
            continue
        if not raw:
            slots[index] = UploadResult(
                ok=False,
                correlation_id=f"{correlation_id}-{index}",
                filename=filename,
                error="empty_file",
            )
            continue

        valid.append((index, raw, filename, media_type))

    log.info(
        "upload.accepted",
        correlation_id=correlation_id,
        foreman=foreman,
        total=len(files),
        valid=len(valid),
        rejected=len(files) - len(valid),
    )

    if valid:
        processed = await process_upd_batch(
            items=[(raw, name, mtype) for _, raw, name, mtype in valid],
            foreman=foreman,
            files=files_svc,
            vision=vision,
            sheets=sheets,
            settings=settings,
            correlation_id=correlation_id,
        )
        for (slot_index, _, _, _), result in zip(valid, processed, strict=True):
            slots[slot_index] = result

    items: list[UploadResult] = [slot for slot in slots if slot is not None]
    batch_ok = all(item.ok and not item.needs_review for item in items)
    return BatchUploadResult(
        ok=batch_ok,
        correlation_id=correlation_id,
        items=items,
    ).model_dump(mode="json")
