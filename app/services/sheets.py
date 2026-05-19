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
from app.models import ReconRow, SheetUPDRow, UPDRecord

log = get_logger("sheets")

# 12-column side-by-side layout. Yellow block = 1С register (filled on
# reconciliation), green block = foreman upload, Status = reconciliation result.
# The bookkeeper maintains the header row manually; the service never touches it.
COLUMNS: tuple[str, ...] = (
    "Дата (1С)",          # A — yellow
    "Контрагент (1С)",    # B — yellow
    "Сумма (1С)",         # C — yellow
    "№ УПД (1С)",         # D — yellow
    "№ УПД",              # E — green
    "Дата",               # F — green
    "Сумма",              # G — green
    "Контрагент",         # H — green
    "Организация",        # I — green
    "Прораб",             # J — green
    "Дата загрузки",      # K — green
    "Статус",             # L — status
)

# Indices used by readers / writers.
_YELLOW_RANGE_TMPL = "A2:D{last_row}"
_GREEN_RANGE_TMPL = "E2:K{last_row}"
_STATUS_RANGE_TMPL = "L2:L{last_row}"
_DATA_CLEAR_RANGE = "A2:L"

# Soft, easy-on-the-eyes background tones.
_YELLOW_BG = {"red": 0.996, "green": 0.910, "blue": 0.651}
_GREEN_BG = {"red": 0.780, "green": 0.918, "blue": 0.788}

_SCOPES = ("https://www.googleapis.com/auth/spreadsheets",)


class SheetsService:
    """Client for the configured Google Sheet.

    Worksheet handle is opened lazily on the first call so that the service
    can be constructed at startup without blocking on a network round-trip.
    """

    def __init__(self, *, credentials_json: str, sheet_id: str) -> None:
        self._credentials_json = credentials_json
        self._sheet_id = sheet_id
        self._worksheet: gspread.Worksheet | None = None

    # --- public API: foreman upload (green-only append) ---------------------

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
        """Append one green-side row to the configured worksheet.

        Yellow columns (1С block) and the Status column are left empty —
        they'll be filled on the next reconciliation. Translates SDK errors
        into :class:`SheetsAppendError`. Blocks on network I/O.
        """
        worksheet = self._get_worksheet()
        values = [
            "",  # A — Дата (1С)
            "",  # B — Контрагент (1С)
            "",  # C — Сумма (1С)
            "",  # D — № УПД (1С)
            record.upd_number or "",                              # E — № УПД
            record.date.isoformat() if record.date else "",       # F — Дата
            record.amount if record.amount is not None else "",   # G — Сумма
            record.counterparty or "",                            # H — Контрагент
            record.organization or "",                            # I — Организация
            foreman,                                              # J — Прораб
            datetime.now(UTC).isoformat(timespec="seconds"),      # K — Дата загрузки
            "",  # L — Статус (filled on reconciliation)
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

    # --- public API: reconciliation (full data rewrite) ---------------------

    async def read_all_records(self) -> list[SheetUPDRow]:
        """Async wrapper over :meth:`read_all_records_sync`."""
        return await asyncio.to_thread(self.read_all_records_sync)

    def read_all_records_sync(self) -> list[SheetUPDRow]:
        """Read every green-side row from the worksheet.

        Skips the header row and any row whose green ``№ УПД`` cell (column E)
        is empty — those are reconciliation "NO" rows or genuinely empty lines.
        SDK errors are translated into :class:`SheetsReadError`.
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
                continue
            log.debug("sheets.read.row", row=idx, upd_number=row.upd_number)
            rows.append(row)

        log.info("sheets.read.done", rows=len(rows))
        return rows

    async def rewrite_reconciliation(self, rows: list[ReconRow]) -> None:
        """Async wrapper over :meth:`rewrite_reconciliation_sync`."""
        await asyncio.to_thread(self.rewrite_reconciliation_sync, rows)

    def rewrite_reconciliation_sync(self, rows: list[ReconRow]) -> None:
        """Clear the data area and rewrite it with the merged 1С/foreman rows.

        Header is preserved; everything below is recomputed every reconciliation.
        After the write, paints yellow/green column backgrounds on the data
        range so the side-by-side layout is visible at a glance. Translates
        SDK errors into :class:`SheetsAppendError`.
        """
        worksheet = self._get_worksheet()
        try:
            worksheet.batch_clear([_DATA_CLEAR_RANGE])
        except gspread.exceptions.APIError as exc:
            log.warning("sheets.rewrite.clear_api_error", error=str(exc))
            raise SheetsAppendError(f"Google Sheets API error: {exc}") from exc
        except Exception as exc:  # pragma: no cover - belt and braces
            log.exception("sheets.rewrite.clear_unexpected")
            raise SheetsAppendError(f"Unexpected error clearing sheet: {exc}") from exc

        if not rows:
            log.info("sheets.rewrite.empty", sheet_id=self._sheet_id)
            return

        values: list[list[object]] = [_recon_row_to_values(r) for r in rows]
        last_row = len(rows) + 1  # +1 because data starts at row 2
        try:
            worksheet.update(
                range_name="A2",
                values=values,
                value_input_option="USER_ENTERED",
            )
        except gspread.exceptions.APIError as exc:
            log.warning("sheets.rewrite.write_api_error", error=str(exc))
            raise SheetsAppendError(f"Google Sheets API error: {exc}") from exc
        except Exception as exc:  # pragma: no cover - belt and braces
            log.exception("sheets.rewrite.write_unexpected")
            raise SheetsAppendError(f"Unexpected error writing rows: {exc}") from exc

        # One batched format() call covers backgrounds + numberFormat for date
        # and amount columns. Without explicit numberFormat the amount columns
        # inherit any "Date" format previously applied to the sheet — Google
        # Sheets would then render 21324 as 1958-05-19.
        date_fmt = {"numberFormat": {"type": "DATE", "pattern": "yyyy-mm-dd"}}
        money_fmt = {"numberFormat": {"type": "NUMBER", "pattern": "#,##0.00"}}
        text_fmt = {"numberFormat": {"type": "TEXT"}}
        try:
            worksheet.batch_format(
                [
                    {
                        "range": _YELLOW_RANGE_TMPL.format(last_row=last_row),
                        "format": {"backgroundColor": _YELLOW_BG},
                    },
                    {
                        "range": _GREEN_RANGE_TMPL.format(last_row=last_row),
                        "format": {"backgroundColor": _GREEN_BG},
                    },
                    {"range": f"A2:A{last_row}", "format": date_fmt},   # Дата (1С)
                    {"range": f"C2:C{last_row}", "format": money_fmt},  # Сумма (1С)
                    {"range": f"D2:D{last_row}", "format": text_fmt},   # № УПД (1С)
                    {"range": f"E2:E{last_row}", "format": text_fmt},   # № УПД
                    {"range": f"F2:F{last_row}", "format": date_fmt},   # Дата
                    {"range": f"G2:G{last_row}", "format": money_fmt},  # Сумма
                    {"range": f"K2:K{last_row}", "format": text_fmt},   # Дата загрузки (ISO)
                ]
            )
        except gspread.exceptions.APIError as exc:
            log.warning("sheets.rewrite.format_api_error", error=str(exc))
            raise SheetsAppendError(f"Google Sheets API error: {exc}") from exc
        except Exception as exc:  # pragma: no cover - belt and braces
            log.exception("sheets.rewrite.format_unexpected")
            raise SheetsAppendError(f"Unexpected error formatting sheet: {exc}") from exc

        log.info(
            "sheets.rewrite.ok",
            rows=len(rows),
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


# --- helpers ----------------------------------------------------------------


def _row_to_sheet_upd(values: list[str], *, source_row: int) -> SheetUPDRow | None:
    """Convert a list-of-strings row into a :class:`SheetUPDRow`.

    Reads the GREEN block (columns E..K, indices 4..10) plus Status (L=11).
    Rows without a green ``№ УПД`` are skipped — they're either "NO" rows
    from the previous reconciliation or genuinely empty.
    """

    def _at(idx: int) -> str:
        return values[idx].strip() if idx < len(values) and values[idx] else ""

    upd_number = _at(4)  # E — № УПД (green)
    if not upd_number:
        return None

    return SheetUPDRow(
        upd_number=upd_number,
        date=_parse_iso_date(_at(5)),         # F — Дата
        amount=_parse_amount(_at(6)),         # G — Сумма
        counterparty=_at(7) or None,          # H — Контрагент
        organization=_at(8) or None,          # I — Организация
        foreman=_at(9) or None,               # J — Прораб
        uploaded_at=_at(10) or None,          # K — Дата загрузки
        status=_at(11) or None,               # L — Статус
        source_row=source_row,
    )


def _recon_row_to_values(row: ReconRow) -> list[object]:
    """Serialise a :class:`ReconRow` into 12 cells in column order."""
    return [
        row.onec_date.isoformat() if row.onec_date else "",                # A
        row.onec_counterparty or "",                                        # B
        row.onec_amount if row.onec_amount is not None else "",             # C
        row.onec_upd_number or "",                                          # D
        row.green_upd_number or "",                                         # E
        row.green_date.isoformat() if row.green_date else "",               # F
        row.green_amount if row.green_amount is not None else "",           # G
        row.green_counterparty or "",                                       # H
        row.green_organization or "",                                       # I
        row.green_foreman or "",                                            # J
        row.green_uploaded_at or "",                                        # K
        row.status,                                                         # L
    ]


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
