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
from app.models import (
    OneCRecord,
    ReconciliationPlan,
    ReconRow,
    SheetUPDRow,
    UPDRecord,
)

log = get_logger("sheets")

# 13-column side-by-side layout. Yellow block = 1С register (filled on
# reconciliation), green block = foreman upload, Status = reconciliation
# result, Файл = Drive link to the original scan (green-side, written on
# upload). The bookkeeper maintains the header row manually; the service never
# touches it. Status stays at column L for backward compatibility with the
# bookkeeper's existing manual statuses — the file link is appended at M.
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
    "Файл",               # M — green (Drive link to scan)
)

# Indices used by readers / writers.
_YELLOW_RANGE_TMPL = "A2:D{last_row}"
_GREEN_RANGE_TMPL = "E2:K{last_row}"
_FILE_RANGE_TMPL = "M2:M{last_row}"

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
        file_url: str | None = None,
    ) -> None:
        """Async wrapper around the synchronous gspread call."""
        await asyncio.to_thread(
            self.append_row_sync,
            record,
            foreman=foreman,
            correlation_id=correlation_id,
            file_url=file_url,
        )

    def append_row_sync(
        self,
        record: UPDRecord,
        *,
        foreman: str,
        correlation_id: str,
        file_url: str | None = None,
    ) -> None:
        """Append one green-side row to the configured worksheet.

        Yellow columns (1С block) and the Status column are left empty —
        they'll be filled on the next reconciliation. ``file_url`` (column M)
        is the Drive link to the archived scan; empty when Drive archival is
        off or failed. Translates SDK errors into :class:`SheetsAppendError`.
        Blocks on network I/O.
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
            file_url or "",  # M — Файл (Drive link to scan)
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

    async def read_onec_records(self) -> list[OneCRecord]:
        """Async wrapper over :meth:`read_onec_records_sync`."""
        return await asyncio.to_thread(self.read_onec_records_sync)

    def read_onec_records_sync(self) -> list[OneCRecord]:
        """Read every 1С (yellow-block) record currently in the worksheet.

        These are the register entries left by previous reconciliations: ``OK`` /
        ``СУММА?`` / ``NO`` rows carry yellow data, ``ЛИШНЕЕ`` rows do not. The
        reconciliation pipeline merges them with a freshly uploaded register so
        old register entries are never dropped just because they fell out of the
        new export. Skips the header and any row whose yellow ``№ УПД`` (column D)
        is empty. SDK errors are translated into :class:`SheetsReadError`.
        """
        log.info("sheets.read_onec.start", sheet_id=self._sheet_id)
        worksheet = self._get_worksheet()
        try:
            raw = worksheet.get_all_values()
        except gspread.exceptions.APIError as exc:
            log.warning("sheets.read_onec.api_error", error=str(exc))
            raise SheetsReadError(f"Google Sheets API error: {exc}") from exc
        except Exception as exc:  # pragma: no cover - defensive
            log.exception("sheets.read_onec.unexpected")
            raise SheetsReadError(f"Unexpected error reading sheet: {exc}") from exc

        rows: list[OneCRecord] = []
        # Row 0 is the human-managed header; data starts at row 1.
        for idx, values in enumerate(raw[1:], start=2):
            row = _row_to_onec(values, source_row=idx)
            if row is None:
                continue
            rows.append(row)

        log.info("sheets.read_onec.done", rows=len(rows))
        return rows

    async def annotate_reconciliation(self, plan: ReconciliationPlan) -> None:
        """Async wrapper over :meth:`annotate_reconciliation_sync`."""
        await asyncio.to_thread(self.annotate_reconciliation_sync, plan)

    def annotate_reconciliation_sync(self, plan: ReconciliationPlan) -> None:
        """Annotate the sheet in place — never clear, never reorder.

        The green foreman uploads are the append-only backbone; this method
        patches the yellow 1С block (A:D) and the Status cell (L) next to each
        matching green row, and appends brand-new ``NO`` rows below the
        existing data. It never calls ``batch_clear`` and never rewrites the
        green block (E:K) or the file link (M), so nothing the bookkeeper
        edited by hand is lost and no row moves or duplicates.

        Three steps, each translating SDK errors into :class:`SheetsAppendError`:

        1. ``batch_update`` the in-place patches (A:D yellow + L status).
        2. ``update`` the appended ``NO`` rows at the first free row below data.
        3. Repaint yellow/green backgrounds + number formats over the data
           block (idempotent — values are untouched, only cell formats).
        """
        worksheet = self._get_worksheet()

        # 1) In-place patches. A ``None`` field on an annotation means "leave it"
        # — that's how a manual status survives (pipeline sets status=None).
        data_updates: list[dict[str, object]] = []
        for ann in plan.annotations:
            if ann.onec is not None:
                data_updates.append(
                    {
                        "range": f"A{ann.source_row}:D{ann.source_row}",
                        "values": [_onec_to_yellow_values(ann.onec)],
                    }
                )
            if ann.status is not None:
                data_updates.append(
                    {"range": f"L{ann.source_row}", "values": [[ann.status]]}
                )

        if data_updates:
            try:
                worksheet.batch_update(
                    data_updates, value_input_option="USER_ENTERED"
                )
            except gspread.exceptions.APIError as exc:
                log.warning("sheets.annotate.update_api_error", error=str(exc))
                raise SheetsAppendError(f"Google Sheets API error: {exc}") from exc
            except Exception as exc:  # pragma: no cover - belt and braces
                log.exception("sheets.annotate.update_unexpected")
                raise SheetsAppendError(
                    f"Unexpected error patching rows: {exc}"
                ) from exc

        # 2) Append new NO rows deterministically below the current data so
        # they never overwrite an existing row or land in a gap.
        appended = plan.appended_rows
        if appended:
            start_row = plan.last_data_row + 1
            values = [_recon_row_to_values(r) for r in appended]
            try:
                worksheet.update(
                    range_name=f"A{start_row}",
                    values=values,
                    value_input_option="USER_ENTERED",
                )
            except gspread.exceptions.APIError as exc:
                log.warning("sheets.annotate.append_api_error", error=str(exc))
                raise SheetsAppendError(f"Google Sheets API error: {exc}") from exc
            except Exception as exc:  # pragma: no cover - belt and braces
                log.exception("sheets.annotate.append_unexpected")
                raise SheetsAppendError(
                    f"Unexpected error appending rows: {exc}"
                ) from exc

        # 3) Delete stale yellow-only NO placeholders that a later upload now
        # matches (otherwise the same УПД shows twice). Delete bottom-up so the
        # remaining row indices stay valid; the data writes above already
        # persisted, so their contents shift up with the rows untouched.
        deleted = sorted(set(plan.deleted_rows), reverse=True)
        for row_number in deleted:
            try:
                worksheet.delete_rows(row_number)
            except gspread.exceptions.APIError as exc:
                log.warning("sheets.annotate.delete_api_error", error=str(exc))
                raise SheetsAppendError(f"Google Sheets API error: {exc}") from exc
            except Exception as exc:  # pragma: no cover - belt and braces
                log.exception("sheets.annotate.delete_unexpected")
                raise SheetsAppendError(
                    f"Unexpected error deleting row: {exc}"
                ) from exc

        # 4) Repaint backgrounds + number formats over the whole data block.
        # Idempotent: only cell formatting changes, never values — so colours
        # the bookkeeper sees stay correct without rebuilding the data. Skip
        # entirely when nothing was written (empty sheet / empty export).
        last_row = plan.last_data_row + len(appended) - len(deleted)
        if not data_updates and not appended and not deleted:
            log.info("sheets.annotate.noop", sheet_id=self._sheet_id)
            return
        if last_row < 2:
            return

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
                    {
                        "range": _FILE_RANGE_TMPL.format(last_row=last_row),
                        "format": {"backgroundColor": _GREEN_BG},
                    },
                    {"range": f"A2:A{last_row}", "format": date_fmt},   # Дата (1С)
                    {"range": f"C2:C{last_row}", "format": money_fmt},  # Сумма (1С)
                    {"range": f"D2:D{last_row}", "format": text_fmt},   # № УПД (1С)
                    {"range": f"E2:E{last_row}", "format": text_fmt},   # № УПД
                    {"range": f"F2:F{last_row}", "format": date_fmt},   # Дата
                    {"range": f"G2:G{last_row}", "format": money_fmt},  # Сумма
                    {"range": f"K2:K{last_row}", "format": text_fmt},   # Дата загрузки (ISO)
                    {"range": f"M2:M{last_row}", "format": text_fmt},   # Файл (URL)
                ]
            )
        except gspread.exceptions.APIError as exc:
            log.warning("sheets.annotate.format_api_error", error=str(exc))
            raise SheetsAppendError(f"Google Sheets API error: {exc}") from exc
        except Exception as exc:  # pragma: no cover - belt and braces
            log.exception("sheets.annotate.format_unexpected")
            raise SheetsAppendError(f"Unexpected error formatting sheet: {exc}") from exc

        log.info(
            "sheets.annotate.ok",
            annotations=len(plan.annotations),
            appended=len(appended),
            deleted=len(deleted),
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
        file_url=_at(12) or None,             # M — Файл
        source_row=source_row,
    )


def _row_to_onec(values: list[str], *, source_row: int) -> OneCRecord | None:
    """Convert a sheet row into an :class:`OneCRecord` from the YELLOW block.

    Reads columns A..D (date, counterparty, amount, № УПД) — the 1С side written
    by the previous reconciliation. Rows without a yellow ``№ УПД`` are skipped:
    those are ``ЛИШНЕЕ`` rows (green only) or genuinely empty.
    """

    def _at(idx: int) -> str:
        return values[idx].strip() if idx < len(values) and values[idx] else ""

    upd_number = _at(3)  # D — № УПД (1С)
    if not upd_number:
        return None

    return OneCRecord(
        upd_number=upd_number,
        date=_parse_iso_date(_at(0)),     # A — Дата (1С)
        organization=_at(1) or None,      # B — Контрагент (1С)
        amount=_parse_amount(_at(2)),     # C — Сумма (1С)
        source_row=source_row,
    )


def _recon_row_to_values(row: ReconRow) -> list[object]:
    """Serialise a :class:`ReconRow` into 13 cells in column order."""
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
        row.green_file_url or "",                                           # M
    ]


def _onec_to_yellow_values(rec: OneCRecord) -> list[object]:
    """Serialise a 1С record into the four yellow cells A:D in column order."""
    return [
        rec.date.isoformat() if rec.date else "",          # A — Дата (1С)
        rec.organization or "",                             # B — Контрагент (1С)
        rec.amount if rec.amount is not None else "",       # C — Сумма (1С)
        rec.upd_number or "",                               # D — № УПД (1С)
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
