"""Stage 1 pipeline: webhook payload → extracted UPD record logged as JSON."""

from __future__ import annotations

from app.core.errors import (
    AppError,
    FileDownloadError,
    UnsupportedFileTypeError,
    VisionExtractionError,
)
from app.core.logging import bind_correlation_id, get_logger
from app.models import WebhookPayload
from app.services.files import FilesService
from app.services.vision import VisionService

log = get_logger("pipeline.upd_upload")


async def process_upd(
    payload: WebhookPayload,
    files: FilesService,
    vision: VisionService,
    *,
    correlation_id: str | None = None,
) -> None:
    """Download → to_png → Claude Vision → log the extracted record.

    Services are injected as parameters (no globals); pipeline never imports
    external SDKs directly. All exceptions are caught and logged — background
    tasks must not crash the event loop, and the webhook response has already
    gone out.
    """
    with bind_correlation_id(correlation_id) as cid:
        log.info(
            "upd.received",
            foreman=payload.foreman,
            file_name=payload.file_name,
            file_url=str(payload.file_url),
            submitted_at=payload.submitted_at.isoformat() if payload.submitted_at else None,
            form_id=payload.form_id,
            correlation_id=cid,
        )

        try:
            raw, media_type = await files.download(str(payload.file_url))
            png = await files.to_png(
                raw, filename=payload.file_name, media_type=media_type
            )
            record = await vision.extract(png.data, media_type=png.media_type)
            record_dump = record.model_dump(mode="json")
            log.info(
                "upd.extracted",
                **record_dump,
                foreman=payload.foreman,
                file_name=payload.file_name,
            )
            if record.needs_review:
                missing = [
                    k
                    for k in ("organization", "date", "amount", "upd_number")
                    if record_dump.get(k) is None
                ]
                log.warning(
                    "upd.needs_review",
                    foreman=payload.foreman,
                    file_name=payload.file_name,
                    missing_fields=missing,
                    note="Документ не распознан полностью — требуется ручная проверка бухгалтером",
                )
        except FileDownloadError as exc:
            log.exception("upd.download_failed", error=str(exc))
        except UnsupportedFileTypeError as exc:
            log.exception("upd.unsupported_file", error=str(exc))
        except VisionExtractionError as exc:
            log.exception("upd.extract_failed", error=str(exc))
        except AppError as exc:  # catch-all for domain errors
            log.exception("upd.app_error", error=str(exc))
        except Exception as exc:  # pragma: no cover - safety net
            log.exception("upd.unexpected_error", error=str(exc))
