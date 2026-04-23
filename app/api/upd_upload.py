"""POST /webhook/yandex-form — entry point for Yandex Forms submissions."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, BackgroundTasks, Depends, status

from app.core.logging import get_logger
from app.deps import get_files_service, get_vision_service, verify_webhook_secret
from app.models import WebhookPayload
from app.pipelines.upd_upload import process_upd
from app.services.files import FilesService
from app.services.vision import VisionService

log = get_logger("api.upd_upload")

router = APIRouter(tags=["upd"])


@router.post(
    "/webhook/yandex-form",
    status_code=status.HTTP_200_OK,
    dependencies=[Depends(verify_webhook_secret)],
)
async def yandex_form_webhook(
    payload: WebhookPayload,
    bg: BackgroundTasks,
    files: FilesService = Depends(get_files_service),
    vision: VisionService = Depends(get_vision_service),
) -> dict[str, object]:
    """Validate payload, schedule background processing, return 200 immediately.

    Yandex Forms retries on timeout/5XX — this handler must stay fast and
    idempotent. All real work happens in ``process_upd`` via BackgroundTasks.
    """
    correlation_id = uuid.uuid4().hex
    log.info(
        "webhook.accepted",
        correlation_id=correlation_id,
        foreman=payload.foreman,
        file_name=payload.file_name,
        form_id=payload.form_id,
    )
    bg.add_task(process_upd, payload, files, vision, correlation_id=correlation_id)
    return {"ok": True, "correlation_id": correlation_id}
