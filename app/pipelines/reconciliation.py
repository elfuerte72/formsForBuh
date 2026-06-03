"""Stage 2 pipeline: 1С export ↔ foreman Google Sheet reconciliation.

Synchronous flow (mirrors :mod:`app.pipelines.upd_upload`): the bookkeeper
clicks «Сравнить», the pipeline computes a side-by-side diff, **rewrites the
data area of the Google Sheet** with paired yellow/green rows and a
``OK / NO / ЛИШНЕЕ`` status, and returns only the high-level counts. The
form is intentionally minimal — the actual list lives in the spreadsheet.
"""

from __future__ import annotations

from collections import defaultdict

from app.core.errors import AppError, OneCParseError, SheetsAppendError, SheetsReadError
from app.core.logging import bind_correlation_id, get_logger
from app.models import (
    OneCRecord,
    ReconRow,
    ReconciliationResult,
    ReconciliationStats,
    SheetUPDRow,
)
from app.services.onec import OneCParserService
from app.services.sheets import SheetsService

log = get_logger("pipeline.reconciliation")


async def reconcile(
    *,
    raw: bytes,
    filename: str,
    onec: OneCParserService,
    sheets: SheetsService,
    correlation_id: str,
) -> ReconciliationResult:
    """Compare a 1С export against the foreman sheet and rewrite the sheet."""
    with bind_correlation_id(correlation_id):
        log.info("reconcile.received", filename=filename, bytes=len(raw))

        try:
            onec_records = onec.parse(raw, filename)
            log.info(
                "reconcile.parsed_onec",
                records=len(onec_records),
                filename=filename,
            )

            sheet_rows = await sheets.read_all_records()
            log.info("reconcile.read_sheet", rows=len(sheet_rows))

            recon_rows, stats = _diff(onec_records, sheet_rows)
            log.info(
                "reconcile.diff",
                matched=stats.matched,
                missing=stats.missing,
                extras=stats.extras,
                amount_mismatch=stats.amount_mismatch,
            )

            await sheets.rewrite_reconciliation(recon_rows)
            log.info("reconcile.sheet_written", rows=len(recon_rows))

            return ReconciliationResult(
                ok=True,
                correlation_id=correlation_id,
                stats=stats,
            )

        except OneCParseError as exc:
            log.warning("reconcile.onec_parse_failed", error=str(exc))
            return ReconciliationResult(
                ok=False,
                correlation_id=correlation_id,
                error="onec_parse_error",
            )
        except SheetsReadError as exc:
            log.exception("reconcile.sheets_read_failed", error=str(exc))
            return ReconciliationResult(
                ok=False,
                correlation_id=correlation_id,
                error="sheets_read_error",
            )
        except SheetsAppendError as exc:
            log.exception("reconcile.sheets_write_failed", error=str(exc))
            return ReconciliationResult(
                ok=False,
                correlation_id=correlation_id,
                error="sheets_write_error",
            )
        except AppError as exc:
            log.exception("reconcile.app_error", error=str(exc))
            return ReconciliationResult(
                ok=False,
                correlation_id=correlation_id,
                error="app_error",
            )
        except Exception as exc:  # pragma: no cover - safety net
            log.exception("reconcile.unexpected_error", error=str(exc))
            return ReconciliationResult(
                ok=False,
                correlation_id=correlation_id,
                error="unexpected_error",
            )


async def pending_uploads(
    *,
    sheets: SheetsService,
    correlation_id: str,
) -> int:
    """Count foreman uploads that have not been reconciled yet.

    A fresh :meth:`SheetsService.append_row` writes a green-only row and leaves
    the Status column empty; reconciliation always stamps a status. So a green
    row with an empty status is an upload that landed *after* the last «Сводка»
    and is not yet paired with its 1С counterpart. Read failures degrade to
    ``0`` — the hint is advisory, never a hard error.
    """
    with bind_correlation_id(correlation_id):
        try:
            rows = await sheets.read_all_records()
        except AppError as exc:
            # Any read / worksheet-open failure → no hint rather than an error.
            log.warning("reconcile.pending_read_failed", error=str(exc))
            return 0
        pending = sum(1 for row in rows if not row.status)
        log.info("reconcile.pending", pending=pending, total=len(rows))
        return pending


# --- diff -------------------------------------------------------------------


def _diff(
    onec_records: list[OneCRecord],
    sheet_rows: list[SheetUPDRow],
) -> tuple[list[ReconRow], ReconciliationStats]:
    """Build the side-by-side row list + summary counts.

    Comparison key: :func:`_normalize` applied to ``upd_number``. Same
    function on both sides. Pairing strategy:

    - one row per foreman upload (preserves duplicates as separate ``OK``
      rows in the sheet, both pointing at the same 1С record);
    - 1С records that no foreman uploaded → ``NO`` rows (yellow only);
    - foreman uploads without a 1С match → ``ЛИШНЕЕ`` rows (green only).
    """
    onec_index: dict[str, OneCRecord] = {}
    for rec in onec_records:
        key = _normalize(rec.upd_number)
        if not key:
            continue
        if key in onec_index:
            log.warning(
                "reconcile.onec_duplicate_key",
                key=key,
                first_row=onec_index[key].source_row,
                duplicate_row=rec.source_row,
            )
            continue
        onec_index[key] = rec

    matched_keys: set[str] = set()
    rows: list[ReconRow] = []
    matched = 0
    amount_mismatch = 0
    extras = 0

    foreman_by_key: dict[str, list[SheetUPDRow]] = defaultdict(list)
    for row in sheet_rows:
        key = _normalize(row.upd_number)
        if not key:
            continue
        foreman_by_key[key].append(row)

    for key, group in foreman_by_key.items():
        onec_rec = onec_index.get(key)
        if onec_rec is not None:
            matched_keys.add(key)
            for row in group:
                recon_row = _make_matched_row(onec_rec, row)
                rows.append(recon_row)
                if recon_row.status == "OK":
                    matched += 1
                else:  # "СУММА?" — number matched but amounts diverge.
                    amount_mismatch += 1
                    log.info(
                        "reconcile.amount_mismatch",
                        key=key,
                        onec_amount=onec_rec.amount,
                        green_amount=row.amount,
                        onec_row=onec_rec.source_row,
                        green_row=row.source_row,
                    )
        else:
            for row in group:
                rows.append(_make_extra_row(row))
                extras += 1

    # Remaining 1С records → NO rows (foreman has not uploaded them yet).
    missing = 0
    no_rows: list[ReconRow] = []
    for key, rec in onec_index.items():
        if key in matched_keys:
            continue
        no_rows.append(_make_no_row(rec))
        missing += 1

    # Order: foreman uploads (OK / СУММА? / ЛИШНЕЕ) on top in the order they
    # appeared in the sheet (preserves source_row), NO rows last so the "still
    # missing" list sits in its own block at the bottom. ``rows`` never holds a
    # NO row by construction; the filter just makes the intent explicit.
    sorted_rows = [r for r in rows if r.status != "NO"] + no_rows

    stats = ReconciliationStats(
        matched=matched,
        missing=missing,
        extras=extras,
        amount_mismatch=amount_mismatch,
    )
    return sorted_rows, stats


def _make_matched_row(onec_rec: OneCRecord, foreman_row: SheetUPDRow) -> ReconRow:
    """Pair a 1С record with a foreman upload that shares its UPD number.

    Status is ``OK`` when the amounts agree (within :data:`_AMOUNT_TOLERANCE`)
    and ``СУММА?`` when they diverge — the number matched but one side's amount
    was likely misread, so the bookkeeper should eyeball it.
    """
    status = "OK" if _amounts_agree(onec_rec.amount, foreman_row.amount) else "СУММА?"
    return ReconRow(
        status=status,
        onec_date=onec_rec.date,
        onec_counterparty=onec_rec.organization,
        onec_amount=onec_rec.amount,
        onec_upd_number=onec_rec.upd_number,
        green_upd_number=foreman_row.upd_number,
        green_date=foreman_row.date,
        green_amount=foreman_row.amount,
        green_counterparty=foreman_row.counterparty,
        green_organization=foreman_row.organization,
        green_foreman=foreman_row.foreman,
        green_uploaded_at=foreman_row.uploaded_at,
    )


def _make_no_row(onec_rec: OneCRecord) -> ReconRow:
    return ReconRow(
        status="NO",
        onec_date=onec_rec.date,
        onec_counterparty=onec_rec.organization,
        onec_amount=onec_rec.amount,
        onec_upd_number=onec_rec.upd_number,
    )


def _make_extra_row(foreman_row: SheetUPDRow) -> ReconRow:
    return ReconRow(
        status="ЛИШНЕЕ",
        green_upd_number=foreman_row.upd_number,
        green_date=foreman_row.date,
        green_amount=foreman_row.amount,
        green_counterparty=foreman_row.counterparty,
        green_organization=foreman_row.organization,
        green_foreman=foreman_row.foreman,
        green_uploaded_at=foreman_row.uploaded_at,
    )


# Amounts are rubles with kopecks; a 1-kopeck gap is float noise, not a real
# discrepancy. Anything larger is flagged as «СУММА?».
_AMOUNT_TOLERANCE = 0.01


def _amounts_agree(onec_amount: float | None, green_amount: float | None) -> bool:
    """True when the two amounts match within tolerance.

    If either side is missing we cannot judge a discrepancy, so we treat it as
    agreeing — a missing amount is a separate problem (caught at upload time).
    """
    if onec_amount is None or green_amount is None:
        return True
    return abs(onec_amount - green_amount) <= _AMOUNT_TOLERANCE


def _normalize(value: str | None) -> str:
    """Lower → strip → drop spaces → drop leading zeros."""
    if value is None:
        return ""
    text = str(value).strip().lower().replace("\xa0", "")
    text = "".join(ch for ch in text if not ch.isspace())
    text = text.lstrip("0")
    return text
