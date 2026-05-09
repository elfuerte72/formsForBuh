"""Google Sheets service (SDK boundary: gspread + google-auth).

This module is the ONLY place that imports ``gspread`` and ``google.oauth2``.
SDK exceptions are translated into :class:`SheetsAppendError` /
:class:`SheetsReadError` so pipelines keep handling a single error taxonomy.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, date as Date, datetime

import gspread
from google.oauth2.service_account import Credentials

from app.core.errors import SheetsAppendError, SheetsReadError
from app.core.logging import get_logger
from app.models import SheetUPDRow, UPDRecord

log = get_logger("sheets")

# Order of columns the service writes — keep the spreadsheet header row in sync.
# «Статус» is left blank by the service and filled in manually by the bookkeeper.
COLUMNS: tuple[str, ...] = (
    "Организация",
    "Контрагент",
    "Дата",
    "Сумма",
    "Номер УПД",
    "Прораб",
    "Дата загрузки",
    "Статус",
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
            record.counterparty or "",
            record.date.isoformat() if record.date else "",
            record.amount if record.amount is not None else "",
            record.upd_number or "",
            foreman,
            datetime.now(UTC).isoformat(timespec="seconds"),
            "",  # «Статус» — bookkeeper fills it in by hand
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

    async def read_all_records(self) -> list[SheetUPDRow]:
        """Async wrapper over :meth:`read_all_records_sync`."""
        return await asyncio.to_thread(self.read_all_records_sync)

    def read_all_records_sync(self) -> list[SheetUPDRow]:
        """Read every data row from the configured worksheet.

        Skips the header row (row 1, the manually-created column titles)
        and any row whose ``Номер УПД`` cell is empty. ``gspread`` errors
        are translated into :class:`SheetsReadError`.
        """
        log.info("sheets.read.start", sheet_id=self._sheet_id)
        worksheet = self._get_worksheet()
        try:
            raw = worksheet.get_all_values()
        except gspread.exceptions.APIError as exc:
            log.warning("sheets.read.api_error", error=str(exc))
            raise SheetsReadError(f"Google Sheets API error: {exc}") from exc
        except Exception as exc:  # pragma: no cover - defensive
            log.exception("sheets.read.unexpected")
            raise SheetsReadError(f"Unexpected error reading sheet: {exc}") from exc

        rows: list[SheetUPDRow] = []
        # Row 0 is the human-managed header; data starts at row 1.
        for idx, values in enumerate(raw[1:], start=2):
            row = _row_to_sheet_upd(values, source_row=idx)
            if row is None:
                log.warning("sheets.read.skip_row", row=idx, reason="empty upd_number")
                continue
            log.debug("sheets.read.row", row=idx, upd_number=row.upd_number)
            rows.append(row)

        log.info("sheets.read.done", rows=len(rows))
        return rows

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


# --- helpers ----------------------------------------------------------------


def _row_to_sheet_upd(values: list[str], *, source_row: int) -> SheetUPDRow | None:
    """Convert a list-of-strings row into a :class:`SheetUPDRow`.

    Column order matches :data:`COLUMNS`. Returns ``None`` when the row
    has no UPD number — the caller is expected to skip it with a warning.
    """

    def _at(idx: int) -> str:
        return values[idx].strip() if idx < len(values) and values[idx] else ""

    upd_number = _at(4)
    if not upd_number:
        return None

    return SheetUPDRow(
        organization=_at(0) or None,
        counterparty=_at(1) or None,
        date=_parse_iso_date(_at(2)),
        amount=_parse_amount(_at(3)),
        upd_number=upd_number,
        foreman=_at(5) or None,
        uploaded_at=_at(6) or None,
        status=_at(7) or None,
        source_row=source_row,
    )


def _parse_iso_date(value: str) -> Date | None:
    if not value:
        return None
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    return None


def _parse_amount(value: str) -> float | None:
    if not value:
        return None
    candidate = value.replace("\xa0", "").replace(" ", "").replace(",", ".")
    try:
        return float(candidate)
    except ValueError:
        return None
