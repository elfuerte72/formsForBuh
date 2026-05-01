"""Stage 2 pipeline: 1С export ↔ foreman Google Sheet reconciliation.

Synchronous flow (mirrors :mod:`app.pipelines.upd_upload`): the bookkeeper
clicks «Сравнить» and the UI waits for the response. Errors raised by
services are translated into :class:`ReconciliationResult` with
``ok=False`` so the handler can render an error banner.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date as Date
from typing import Iterable

from app.core.errors import AppError, OneCParseError, SheetsReadError
from app.core.logging import bind_correlation_id, get_logger
from app.models import (
    DuplicateUPD,
    ExtraUPD,
    MissingUPD,
    OneCRecord,
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
    """Compare a 1С export against the foreman Google Sheet.

    Returns a :class:`ReconciliationResult` describing three lists:

    - ``missing`` — UPDs present in 1С but never uploaded by foremen.
    - ``duplicates`` — UPDs uploaded more than once.
    - ``extras`` — UPDs uploaded by a foreman that have no match in 1С.
    """
    with bind_correlation_id(correlation_id):
        log.info(
            "reconcile.received",
            filename=filename,
            bytes=len(raw),
        )

        try:
            onec_records = onec.parse(raw, filename)
            log.info(
                "reconcile.parsed_onec",
                records=len(onec_records),
                filename=filename,
            )

            sheet_rows = await sheets.read_all_records()
            log.info("reconcile.read_sheet", rows=len(sheet_rows))

            missing, duplicates, extras, stats = _diff(onec_records, sheet_rows)
            log.info(
                "reconcile.diff",
                onec_total=stats.onec_total,
                foreman_total=stats.foreman_total,
                matched=stats.matched,
                missing=stats.missing,
                duplicates=stats.duplicates,
                extras=stats.extras,
                coverage_percent=stats.coverage_percent,
            )

            return ReconciliationResult(
                ok=True,
                correlation_id=correlation_id,
                missing=missing,
                duplicates=duplicates,
                extras=extras,
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


# --- diff -------------------------------------------------------------------


def _diff(
    onec_records: list[OneCRecord],
    sheet_rows: list[SheetUPDRow],
) -> tuple[
    list[MissingUPD],
    list[DuplicateUPD],
    list[ExtraUPD],
    ReconciliationStats,
]:
    """Build the three reconciliation lists + summary stats.

    Comparison key: :func:`_normalize` applied to ``upd_number``. The same
    function is used for both sides; one normalised number is enough for
    a single-counterparty workflow (see plan, «Out of Scope» for fuzzy
    matching).
    """
    onec_index: dict[str, OneCRecord] = {}
    for rec in onec_records:
        key = _normalize(rec.upd_number)
        log.debug(
            "reconcile.normalize",
            side="onec",
            raw=rec.upd_number,
            key=key,
            row=rec.source_row,
        )
        if not key:
            continue
        onec_index.setdefault(key, rec)

    foreman_groups: dict[str, list[SheetUPDRow]] = defaultdict(list)
    for row in sheet_rows:
        key = _normalize(row.upd_number)
        log.debug(
            "reconcile.normalize",
            side="foreman",
            raw=row.upd_number,
            key=key,
            row=row.source_row,
        )
        if not key:
            continue
        foreman_groups[key].append(row)

    missing = [
        MissingUPD(
            upd_number=rec.upd_number,
            date=rec.date,
            amount=rec.amount,
            organization=rec.organization,
            source_row=rec.source_row,
        )
        for key, rec in onec_index.items()
        if key not in foreman_groups
    ]

    duplicates = [
        DuplicateUPD(
            upd_number=rows[0].upd_number,
            count=len(rows),
            foremen=[r.foreman for r in rows if r.foreman],
            dates=[r.date for r in rows if r.date],
        )
        for rows in foreman_groups.values()
        if len(rows) >= 2
    ]

    extras = [
        ExtraUPD(
            upd_number=rows[0].upd_number,
            foreman=rows[0].foreman,
            date=rows[0].date,
        )
        for key, rows in foreman_groups.items()
        if key not in onec_index
    ]

    matched = sum(1 for key in foreman_groups if key in onec_index)
    onec_total = len(onec_records)
    foreman_total = len(sheet_rows)
    coverage = (matched / onec_total * 100) if onec_total else 0.0

    stats = ReconciliationStats(
        onec_total=onec_total,
        foreman_total=foreman_total,
        matched=matched,
        missing=len(missing),
        duplicates=len(duplicates),
        extras=len(extras),
        coverage_percent=round(coverage, 1),
    )
    return _sort_missing(missing), duplicates, extras, stats


_MIN_DATE = Date.min


def _sort_missing(items: Iterable[MissingUPD]) -> list[MissingUPD]:
    return sorted(items, key=lambda m: (m.date or _MIN_DATE, m.source_row))


def _normalize(value: str | None) -> str:
    """Lower → strip → drop spaces → drop leading zeros.

    Matching contract is documented in the plan (Phase 3, T6). The
    operations are commutative for ASCII input but stable and cheap; we
    intentionally do *not* try fuzzy matching at this stage.
    """
    if value is None:
        return ""
    text = str(value).strip().lower().replace("\xa0", "")
    text = "".join(ch for ch in text if not ch.isspace())
    text = text.lstrip("0")
    return text
