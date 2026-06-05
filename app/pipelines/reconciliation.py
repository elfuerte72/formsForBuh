"""Stage 2 pipeline: 1С export ↔ foreman Google Sheet reconciliation.

Synchronous flow (mirrors :mod:`app.pipelines.upd_upload`): the bookkeeper
clicks «Сравнить», the pipeline computes a side-by-side diff, **rewrites the
data area of the Google Sheet** with paired yellow/green rows and a
``OK / NO / ЛИШНЕЕ`` status, and returns only the high-level counts. The
form is intentionally minimal — the actual list lives in the spreadsheet.

Register entries accumulate across reconciliations: before diffing, the
pipeline reads the 1С rows already in the sheet and merges them with the new
export (:func:`_combine_onec`), so an export covering only a recent window
never wipes register rows seen in earlier weeks.
"""

from __future__ import annotations

from app.core.errors import AppError, OneCParseError, SheetsAppendError, SheetsReadError
from app.core.logging import bind_correlation_id, get_logger
from app.models import (
    OneCRecord,
    ReconciliationPlan,
    ReconciliationResult,
    ReconciliationStats,
    ReconRow,
    RowAnnotation,
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

            existing_onec = await sheets.read_onec_records()
            combined_onec = _combine_onec(new=onec_records, existing=existing_onec)
            log.info(
                "reconcile.merged_onec",
                new=len(onec_records),
                existing=len(existing_onec),
                carried_over=len(combined_onec) - len(onec_records),
            )

            plan, stats = _build_plan(combined_onec, sheet_rows, existing_onec)
            log.info(
                "reconcile.diff",
                matched=stats.matched,
                missing=stats.missing,
                extras=stats.extras,
                amount_mismatch=stats.amount_mismatch,
                annotations=len(plan.annotations),
                appended=len(plan.appended_rows),
                deleted=len(plan.deleted_rows),
            )

            await sheets.annotate_reconciliation(plan)
            log.info(
                "reconcile.sheet_written",
                annotations=len(plan.annotations),
                appended=len(plan.appended_rows),
                deleted=len(plan.deleted_rows),
            )

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


# --- merge ------------------------------------------------------------------


def _combine_onec(
    *, new: list[OneCRecord], existing: list[OneCRecord]
) -> list[OneCRecord]:
    """Merge a freshly parsed register with the one already in the sheet.

    The 1С register the bookkeeper exports may only cover a recent window, so a
    naive rewrite would drop every register entry that fell out of that window.
    To preserve history we carry previously-seen register rows forward: a new
    record wins on a key clash (the fresh export is authoritative — an amount may
    have been corrected), and any old record whose number is absent from the new
    export is kept unchanged. Matching key is :func:`_normalize` on both sides;
    new records stay first so :func:`_diff` (first-wins) keeps the fresh copy.
    """
    new_keys = {_normalize(rec.upd_number) for rec in new}
    new_keys.discard("")
    combined = list(new)
    for rec in existing:
        key = _normalize(rec.upd_number)
        if not key or key in new_keys:
            continue
        combined.append(rec)
    return combined


# --- status protection (variant B: auto-marker) -----------------------------

# Auto-statuses written by reconciliation carry this marker so the next run can
# tell them apart from a status the bookkeeper set by hand. A status WITHOUT the
# marker is treated as manual and is never overwritten — that's how «захожу в
# документ… ставлю OK» survives every later reconciliation.
_AUTO_MARK = "·авто"


def _auto(base: str) -> str:
    """Tag a computed status as auto-generated (e.g. ``"OK"`` → ``"OK·авто"``)."""
    return f"{base}{_AUTO_MARK}"


def _is_auto(status: str | None) -> bool:
    """True when a status was written by a previous reconciliation."""
    return bool(status) and status.endswith(_AUTO_MARK)


def _resolve_status(current: str | None, base: str) -> str | None:
    """Decide what to write into a green row's Status cell (column L).

    - empty / auto-marked → write the freshly computed status with the marker;
    - manual (non-empty, no marker) → return ``None`` so the service leaves the
      cell exactly as the bookkeeper set it.
    """
    if current and not _is_auto(current):
        return None
    return _auto(base)


# --- plan -------------------------------------------------------------------


def _build_plan(
    combined_onec: list[OneCRecord],
    green_rows: list[SheetUPDRow],
    existing_onec: list[OneCRecord],
) -> tuple[ReconciliationPlan, ReconciliationStats]:
    """Build a non-destructive write plan + summary counts.

    Comparison key: :func:`_normalize` applied to ``upd_number`` on both sides.

    The green foreman rows are the append-only backbone: each is annotated in
    place with the matching 1С block (A:D) and a status (``OK`` / ``СУММА?`` /
    ``ЛИШНЕЕ``), respecting any manual status. 1С records nobody uploaded become
    ``NO`` — refreshed in place if a yellow-only placeholder already exists for
    that number, otherwise appended below. A placeholder a later upload now
    matches is deleted so the same УПД never shows twice. Foreman duplicates of
    the same number stay as separate annotated rows.
    """
    onec_index: dict[str, OneCRecord] = {}
    for rec in combined_onec:
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

    green_source_rows = {row.source_row for row in green_rows}
    # Existing yellow-only rows (previous NO placeholders), keyed by number.
    # Rows whose yellow side sits next to a green upload (type-b OK/СУММА? rows)
    # share that green row's source_row and are handled in the green loop, so
    # they're excluded here.
    yellow_only_by_key: dict[str, OneCRecord] = {}
    for rec in existing_onec:
        if rec.source_row in green_source_rows:
            continue
        key = _normalize(rec.upd_number)
        if key and key not in yellow_only_by_key:
            yellow_only_by_key[key] = rec

    annotations: list[RowAnnotation] = []
    matched_keys: set[str] = set()
    matched = 0
    amount_mismatch = 0
    extras = 0

    # Backbone: one annotation per green upload row (duplicates preserved).
    for row in green_rows:
        key = _normalize(row.upd_number)
        if not key:
            continue
        onec_rec = onec_index.get(key)
        if onec_rec is not None:
            matched_keys.add(key)
            if _amounts_agree(onec_rec.amount, row.amount):
                matched += 1
                base = "OK"
            else:
                amount_mismatch += 1
                base = "СУММА?"
                log.info(
                    "reconcile.amount_mismatch",
                    key=key,
                    onec_amount=onec_rec.amount,
                    green_amount=row.amount,
                    onec_row=onec_rec.source_row,
                    green_row=row.source_row,
                )
            annotations.append(
                RowAnnotation(
                    source_row=row.source_row,
                    onec=onec_rec,
                    status=_resolve_status(row.status, base),
                )
            )
        else:
            extras += 1
            annotations.append(
                RowAnnotation(
                    source_row=row.source_row,
                    onec=None,  # no 1С match — leave the yellow block empty
                    status=_resolve_status(row.status, "ЛИШНЕЕ"),
                )
            )

    # 1С records nobody uploaded → NO. Refresh an existing yellow-only row in
    # place (amount may have been corrected), else append a fresh NO row below.
    appended: list[ReconRow] = []
    missing = 0
    for key, rec in onec_index.items():
        if key in matched_keys:
            continue
        missing += 1
        existing = yellow_only_by_key.get(key)
        if existing is not None:
            annotations.append(
                RowAnnotation(
                    source_row=existing.source_row,
                    onec=rec,
                    status=_auto("NO"),
                )
            )
        else:
            appended.append(_make_no_row(rec))

    # Stale yellow-only NO placeholders now matched by a green upload → delete.
    deleted_rows = [
        rec.source_row
        for key, rec in yellow_only_by_key.items()
        if key in matched_keys
    ]

    plan = ReconciliationPlan(
        annotations=annotations,
        appended_rows=appended,
        deleted_rows=deleted_rows,
        last_data_row=_last_data_row(green_rows, existing_onec),
    )
    stats = ReconciliationStats(
        matched=matched,
        missing=missing,
        extras=extras,
        amount_mismatch=amount_mismatch,
    )
    return plan, stats


def _last_data_row(
    green_rows: list[SheetUPDRow], existing_onec: list[OneCRecord]
) -> int:
    """Highest occupied data row (1 = header only, no data)."""
    rows = [r.source_row for r in green_rows] + [r.source_row for r in existing_onec]
    return max(rows) if rows else 1


def _make_no_row(onec_rec: OneCRecord) -> ReconRow:
    """A brand-new yellow-only ``NO`` row to append below existing data."""
    return ReconRow(
        status=_auto("NO"),
        onec_date=onec_rec.date,
        onec_counterparty=onec_rec.organization,
        onec_amount=onec_rec.amount,
        onec_upd_number=onec_rec.upd_number,
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
