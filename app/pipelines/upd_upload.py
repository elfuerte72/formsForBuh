"""Stage 1 pipeline: uploaded UPD → Vision extraction → Google Sheets row.

Synchronous flow (the HTTP handler awaits the full pipeline before responding):
the user is shown the result on the form. All exceptions are translated into
:class:`UploadResult` with ``ok=False`` so the handler can render an error banner.
"""

from __future__ import annotations

from app.config import Settings
from app.core.errors import (
    AppError,
    RateLimitExceededError,
    SheetsAppendError,
    UnsupportedFileTypeError,
    VisionExtractionError,
)
from app.core.logging import bind_correlation_id, get_logger
from app.models import UploadResult
from app.services.files import FilesService
from app.services.sheets import SheetsService
from app.services.vision import VisionService

log = get_logger("pipeline.upd_upload")


async def process_upd(
    *,
    raw: bytes,
    filename: str,
    media_type: str,
    foreman: str,
    files: FilesService,
    vision: VisionService,
    sheets: SheetsService,
    settings: Settings,
    correlation_id: str,
) -> UploadResult:
    """Normalise → Vision → (maybe) Sheets → return :class:`UploadResult`.

    Decision rules:
    - ``record.needs_review`` (any required field is None): row is NOT written
      to Sheets. Returned result has ``ok=True, needs_review=True`` so the
      frontend shows a warning. Bookkeeper investigates from the logs.
    - All four fields present: row is appended, ``sheet_url`` returned.
    - SDK error from any service: ``ok=False`` with a stable machine code
      in ``error`` (``unsupported_file_type``, ``vision_extraction_error``,
      ``sheets_append_error``, ``app_error``, ``unexpected_error``).
    """
    with bind_correlation_id(correlation_id):
        log.info(
            "upd.received",
            foreman=foreman,
            filename=filename,
            media_type=media_type,
            bytes=len(raw),
        )

        try:
            png = await files.to_png(raw, filename=filename, media_type=media_type)
            record = await vision.extract(png.data, media_type=png.media_type)
            record_dump = record.model_dump(mode="json")
            log.info(
                "upd.extracted",
                **{k: v for k, v in record_dump.items() if k != "needs_review"},
                foreman=foreman,
                filename=filename,
            )

            if record.needs_review:
                missing = record.missing_fields()
                log.warning(
                    "upd.needs_review",
                    foreman=foreman,
                    filename=filename,
                    missing_fields=missing,
                )
                return UploadResult(
                    ok=True,
                    correlation_id=correlation_id,
                    record=record,
                    needs_review=True,
                    missing_fields=missing,
                )

            await sheets.append_row(record, foreman=foreman, correlation_id=correlation_id)
            return UploadResult(
                ok=True,
                correlation_id=correlation_id,
                record=record,
                sheet_url=settings.sheet_url,
                needs_review=False,
            )

        except UnsupportedFileTypeError as exc:
            log.warning("upd.unsupported_file", error=str(exc))
            return UploadResult(
                ok=False, correlation_id=correlation_id, error="unsupported_file_type"
            )
        except RateLimitExceededError as exc:
            log.warning("upd.rate_limited", error=str(exc))
            return UploadResult(
                ok=False, correlation_id=correlation_id, error="rate_limit_error"
            )
        except VisionExtractionError as exc:
            log.exception("upd.extract_failed", error=str(exc))
            return UploadResult(
                ok=False, correlation_id=correlation_id, error="vision_extraction_error"
            )
        except SheetsAppendError as exc:
            log.exception("upd.sheets_failed", error=str(exc))
            return UploadResult(
                ok=False, correlation_id=correlation_id, error="sheets_append_error"
            )
        except AppError as exc:
            log.exception("upd.app_error", error=str(exc))
            return UploadResult(ok=False, correlation_id=correlation_id, error="app_error")
        except Exception as exc:  # pragma: no cover - safety net
            log.exception("upd.unexpected_error", error=str(exc))
            return UploadResult(
                ok=False, correlation_id=correlation_id, error="unexpected_error"
            )


async def process_upd_batch(
    *,
    items: list[tuple[bytes, str, str]],
    foreman: str,
    files: FilesService,
    vision: VisionService,
    sheets: SheetsService,
    settings: Settings,
    correlation_id: str,
) -> list[UploadResult]:
    """Run :func:`process_upd` sequentially over a list of (raw, filename, media_type).

    Sequential — not ``asyncio.gather`` — to avoid hitting Anthropic rate limits
    and to keep ``gspread.append_row`` writes ordered. Each child call gets a
    derived correlation id (``"<batch>-<i>"``) so a single batch can be grepped
    end-to-end. The ``filename`` is propagated onto each returned result.
    """
    results: list[UploadResult] = []
    for index, (raw, filename, media_type) in enumerate(items):
        sub_id = f"{correlation_id}-{index}"
        result = await process_upd(
            raw=raw,
            filename=filename,
            media_type=media_type,
            foreman=foreman,
            files=files,
            vision=vision,
            sheets=sheets,
            settings=settings,
            correlation_id=sub_id,
        )
        results.append(result.model_copy(update={"filename": filename}))
    return results
