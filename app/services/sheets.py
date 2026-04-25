"""Google Sheets append service (SDK boundary: gspread + google-auth).

This module is the ONLY place that imports ``gspread`` and ``google.oauth2``.
SDK exceptions are translated into :class:`SheetsAppendError` so pipelines
keep handling a single error taxonomy.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime

import gspread
from google.oauth2.service_account import Credentials

from app.core.errors import SheetsAppendError
from app.core.logging import get_logger
from app.models import UPDRecord

log = get_logger("sheets")

# Order of columns the service writes — keep the spreadsheet header row in sync.
COLUMNS: tuple[str, ...] = (
    "Организация",
    "Дата",
    "Сумма",
    "Номер УПД",
    "Прораб",
    "Загружено",
    "correlation_id",
)

_SCOPES = ("https://www.googleapis.com/auth/spreadsheets",)


class SheetsService:
    """Append-only client for the configured Google Sheet.

    Worksheet handle is opened lazily on the first call so that the service
    can be constructed at startup without blocking on a network round-trip.
    """

    def __init__(self, *, credentials_json: str, sheet_id: str) -> None:
        self._credentials_json = credentials_json
        self._sheet_id = sheet_id
        self._worksheet: gspread.Worksheet | None = None

    # --- public API ---------------------------------------------------------

    async def append_row(
        self,
        record: UPDRecord,
        *,
        foreman: str,
        correlation_id: str,
    ) -> None:
        """Async wrapper around the synchronous gspread call."""
        await asyncio.to_thread(
            self.append_row_sync,
            record,
            foreman=foreman,
            correlation_id=correlation_id,
        )

    def append_row_sync(
        self,
        record: UPDRecord,
        *,
        foreman: str,
        correlation_id: str,
    ) -> None:
        """Append one row to the configured worksheet.

        Translates ``gspread`` / ``google-auth`` errors into
        :class:`SheetsAppendError`. Blocks on network I/O — call from a thread.
        """
        worksheet = self._get_worksheet()
        values = [
            record.organization or "",
            record.date.isoformat() if record.date else "",
            record.amount if record.amount is not None else "",
            record.upd_number or "",
            foreman,
            datetime.now(UTC).isoformat(timespec="seconds"),
            correlation_id,
        ]
        try:
            worksheet.append_row(values, value_input_option="USER_ENTERED")
        except gspread.exceptions.APIError as exc:
            log.warning("sheets.append.api_error", error=str(exc))
            raise SheetsAppendError(f"Google Sheets API error: {exc}") from exc
        except Exception as exc:  # pragma: no cover - belt and braces
            log.exception("sheets.append.unexpected")
            raise SheetsAppendError(f"Unexpected error appending row: {exc}") from exc

        log.info(
            "sheets.append.ok",
            correlation_id=correlation_id,
            foreman=foreman,
            sheet_id=self._sheet_id,
        )

    # --- internal -----------------------------------------------------------

    def _get_worksheet(self) -> gspread.Worksheet:
        """Open + cache the first worksheet of the configured spreadsheet."""
        if self._worksheet is not None:
            return self._worksheet
        try:
            info = json.loads(self._credentials_json)
        except json.JSONDecodeError as exc:
            raise SheetsAppendError(
                "GOOGLE_CREDENTIALS_JSON is not valid JSON"
            ) from exc

        try:
            creds = Credentials.from_service_account_info(info, scopes=list(_SCOPES))
            client = gspread.authorize(creds)
            spreadsheet = client.open_by_key(self._sheet_id)
            self._worksheet = spreadsheet.sheet1
        except Exception as exc:
            log.exception("sheets.open.failed", sheet_id=self._sheet_id)
            raise SheetsAppendError(f"Failed to open spreadsheet: {exc}") from exc
        return self._worksheet
