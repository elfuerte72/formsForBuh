"""Unit tests for the reconciliation pipeline (diff + sheet rewrite)."""

from __future__ import annotations

from datetime import date
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.core.errors import OneCParseError, SheetsAppendError, SheetsReadError
from app.models import OneCRecord, SheetUPDRow
from app.pipelines.reconciliation import reconcile


def _onec(upd_number: str, **overrides) -> OneCRecord:
    base = dict(
        upd_number=upd_number,
        date=date(2026, 3, 1),
        amount=100.0,
        organization="ООО Тест",
        source_row=10,
    )
    base.update(overrides)
    return OneCRecord(**base)


def _sheet(upd_number: str, **overrides) -> SheetUPDRow:
    base = dict(
        upd_number=upd_number,
        organization="Гринлайн",
        counterparty="ООО Тест",
        date=date(2026, 3, 1),
        amount=100.0,
        foreman="Юра",
        uploaded_at=None,
        status=None,
        source_row=2,
    )
    base.update(overrides)
    return SheetUPDRow(**base)


def _services(
    *,
    onec_records=None,
    sheet_rows=None,
    onec_exc=None,
    sheets_exc=None,
    rewrite_exc=None,
):
    onec = MagicMock()
    if onec_exc is not None:
        onec.parse = MagicMock(side_effect=onec_exc)
    else:
        onec.parse = MagicMock(return_value=onec_records or [])

    sheets = MagicMock()
    if sheets_exc is not None:
        sheets.read_all_records = AsyncMock(side_effect=sheets_exc)
    else:
        sheets.read_all_records = AsyncMock(return_value=sheet_rows or [])
    if rewrite_exc is not None:
        sheets.rewrite_reconciliation = AsyncMock(side_effect=rewrite_exc)
    else:
        sheets.rewrite_reconciliation = AsyncMock(return_value=None)

    return onec, sheets


@pytest.mark.asyncio
async def test_perfect_match_writes_ok_rows() -> None:
    onec, sheets = _services(
        onec_records=[_onec("U-1"), _onec("U-2")],
        sheet_rows=[_sheet("U-1"), _sheet("U-2")],
    )
    result = await reconcile(
        raw=b"x",
        filename="r.xls",
        onec=onec,
        sheets=sheets,
        correlation_id="cid",
    )
    assert result.ok is True
    assert result.stats.matched == 2
    assert result.stats.missing == 0
    assert result.stats.extras == 0

    sheets.rewrite_reconciliation.assert_awaited_once()
    written = sheets.rewrite_reconciliation.await_args.args[0]
    assert [r.status for r in written] == ["OK", "OK"]
    assert all(r.onec_upd_number and r.green_upd_number for r in written)


@pytest.mark.asyncio
async def test_missing_emits_no_row_and_counts_in_stats() -> None:
    onec, sheets = _services(
        onec_records=[_onec("U-1"), _onec("U-2", source_row=11)],
        sheet_rows=[_sheet("U-2")],
    )
    result = await reconcile(
        raw=b"x", filename="r.xls", onec=onec, sheets=sheets, correlation_id="cid"
    )
    assert result.ok is True
    assert result.stats.matched == 1
    assert result.stats.missing == 1
    assert result.stats.extras == 0

    written = sheets.rewrite_reconciliation.await_args.args[0]
    statuses = [r.status for r in written]
    assert statuses.count("NO") == 1
    assert statuses.count("OK") == 1
    no_row = next(r for r in written if r.status == "NO")
    assert no_row.onec_upd_number == "U-1"
    assert no_row.green_upd_number is None


@pytest.mark.asyncio
async def test_extras_emit_lishnee_row() -> None:
    onec, sheets = _services(
        onec_records=[_onec("U-1")],
        sheet_rows=[_sheet("U-1"), _sheet("X-9", foreman="Боря")],
    )
    result = await reconcile(
        raw=b"x", filename="r.xls", onec=onec, sheets=sheets, correlation_id="cid"
    )
    assert result.stats.matched == 1
    assert result.stats.extras == 1
    assert result.stats.missing == 0

    written = sheets.rewrite_reconciliation.await_args.args[0]
    extra_rows = [r for r in written if r.status == "ЛИШНЕЕ"]
    assert len(extra_rows) == 1
    assert extra_rows[0].green_upd_number == "X-9"
    assert extra_rows[0].green_foreman == "Боря"
    assert extra_rows[0].onec_upd_number is None


@pytest.mark.asyncio
async def test_foreman_duplicates_produce_multiple_ok_rows() -> None:
    """Two foreman uploads of the same UPD remain as two paired OK rows."""
    onec, sheets = _services(
        onec_records=[_onec("U-1")],
        sheet_rows=[
            _sheet("U-1", foreman="Юра"),
            _sheet("U-1", foreman="Гриша"),
        ],
    )
    result = await reconcile(
        raw=b"x", filename="r.xls", onec=onec, sheets=sheets, correlation_id="cid"
    )
    written = sheets.rewrite_reconciliation.await_args.args[0]
    ok_rows = [r for r in written if r.status == "OK"]
    assert len(ok_rows) == 2
    assert {r.green_foreman for r in ok_rows} == {"Юра", "Гриша"}
    assert result.stats.matched == 2


@pytest.mark.asyncio
async def test_normalization_matches_padding_and_spaces() -> None:
    onec, sheets = _services(
        onec_records=[_onec("00012345")],
        sheet_rows=[_sheet(" 12 345 ")],
    )
    result = await reconcile(
        raw=b"x", filename="r.xls", onec=onec, sheets=sheets, correlation_id="cid"
    )
    assert result.stats.matched == 1
    assert result.stats.missing == 0
    assert result.stats.extras == 0


@pytest.mark.asyncio
async def test_onec_parse_error_returned_as_machine_code() -> None:
    onec, sheets = _services(onec_exc=OneCParseError("broken"))
    result = await reconcile(
        raw=b"x", filename="r.xls", onec=onec, sheets=sheets, correlation_id="cid"
    )
    assert result.ok is False
    assert result.error == "onec_parse_error"
    sheets.read_all_records.assert_not_awaited()
    sheets.rewrite_reconciliation.assert_not_awaited()


@pytest.mark.asyncio
async def test_sheets_read_error_returned_as_machine_code() -> None:
    onec, sheets = _services(
        onec_records=[_onec("U-1")],
        sheets_exc=SheetsReadError("api down"),
    )
    result = await reconcile(
        raw=b"x", filename="r.xls", onec=onec, sheets=sheets, correlation_id="cid"
    )
    assert result.ok is False
    assert result.error == "sheets_read_error"
    sheets.rewrite_reconciliation.assert_not_awaited()


@pytest.mark.asyncio
async def test_sheets_write_error_returned_as_machine_code() -> None:
    onec, sheets = _services(
        onec_records=[_onec("U-1")],
        sheet_rows=[_sheet("U-1")],
        rewrite_exc=SheetsAppendError("api down"),
    )
    result = await reconcile(
        raw=b"x", filename="r.xls", onec=onec, sheets=sheets, correlation_id="cid"
    )
    assert result.ok is False
    assert result.error == "sheets_write_error"
