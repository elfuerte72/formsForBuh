"""Unit tests for the reconciliation pipeline (diff + sheet rewrite)."""

from __future__ import annotations

from datetime import date
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.core.errors import OneCParseError, SheetsAppendError, SheetsReadError
from app.models import OneCRecord, SheetUPDRow
from app.pipelines.reconciliation import pending_uploads, reconcile


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
    existing_onec=None,
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
    sheets.read_onec_records = AsyncMock(return_value=existing_onec or [])
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
async def test_amount_mismatch_flags_summa_status_not_ok() -> None:
    """Same number, different amount → СУММА? row + amount_mismatch count."""
    onec, sheets = _services(
        onec_records=[_onec("U-1", amount=15888.0)],
        sheet_rows=[_sheet("U-1", amount=15348.0)],
    )
    result = await reconcile(
        raw=b"x", filename="r.xls", onec=onec, sheets=sheets, correlation_id="cid"
    )
    assert result.stats.matched == 0
    assert result.stats.amount_mismatch == 1
    assert result.stats.missing == 0
    assert result.stats.extras == 0

    written = sheets.rewrite_reconciliation.await_args.args[0]
    row = next(r for r in written if r.status == "СУММА?")
    assert row.onec_amount == 15888.0
    assert row.green_amount == 15348.0
    assert row.onec_upd_number == "U-1" and row.green_upd_number == "U-1"


@pytest.mark.asyncio
async def test_amount_within_tolerance_stays_ok() -> None:
    onec, sheets = _services(
        onec_records=[_onec("U-1", amount=100.00)],
        sheet_rows=[_sheet("U-1", amount=100.009)],  # < 0.01 gap
    )
    result = await reconcile(
        raw=b"x", filename="r.xls", onec=onec, sheets=sheets, correlation_id="cid"
    )
    assert result.stats.matched == 1
    assert result.stats.amount_mismatch == 0


@pytest.mark.asyncio
async def test_missing_amount_does_not_flag_mismatch() -> None:
    onec, sheets = _services(
        onec_records=[_onec("U-1", amount=None)],
        sheet_rows=[_sheet("U-1", amount=500.0)],
    )
    result = await reconcile(
        raw=b"x", filename="r.xls", onec=onec, sheets=sheets, correlation_id="cid"
    )
    assert result.stats.matched == 1
    assert result.stats.amount_mismatch == 0


@pytest.mark.asyncio
async def test_pending_uploads_counts_rows_without_status() -> None:
    sheets = MagicMock()
    sheets.read_all_records = AsyncMock(
        return_value=[
            _sheet("U-1", status=None),       # fresh upload
            _sheet("U-2", status=""),         # fresh upload (empty string)
            _sheet("U-3", status="OK"),       # already reconciled
        ]
    )
    count = await pending_uploads(sheets=sheets, correlation_id="cid")
    assert count == 2


@pytest.mark.asyncio
async def test_pending_uploads_returns_zero_on_read_error() -> None:
    sheets = MagicMock()
    sheets.read_all_records = AsyncMock(side_effect=SheetsReadError("api down"))
    count = await pending_uploads(sheets=sheets, correlation_id="cid")
    assert count == 0


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
async def test_previous_register_records_are_carried_over() -> None:
    """Old 1С entries absent from the new export survive as NO rows.

    Regression: the bookkeeper's weekly export may only cover a recent window;
    a naive rewrite would wipe register rows from earlier reconciliations.
    """
    onec, sheets = _services(
        onec_records=[_onec("U-2")],                  # new export carries only U-2
        sheet_rows=[],                                 # foreman uploaded nothing
        existing_onec=[_onec("U-1", source_row=2)],   # U-1 seen last week
    )
    result = await reconcile(
        raw=b"x", filename="r.xls", onec=onec, sheets=sheets, correlation_id="cid"
    )
    written = sheets.rewrite_reconciliation.await_args.args[0]
    assert {r.onec_upd_number for r in written} == {"U-1", "U-2"}
    assert all(r.status == "NO" for r in written)
    assert result.stats.missing == 2


@pytest.mark.asyncio
async def test_carried_over_register_record_matches_fresh_upload() -> None:
    """A foreman upload pairs with a register row carried over from last week."""
    onec, sheets = _services(
        onec_records=[],                               # nothing in the new export
        sheet_rows=[_sheet("U-1")],                    # foreman uploaded U-1 now
        existing_onec=[_onec("U-1", source_row=2)],    # U-1 was in an earlier export
    )
    result = await reconcile(
        raw=b"x", filename="r.xls", onec=onec, sheets=sheets, correlation_id="cid"
    )
    written = sheets.rewrite_reconciliation.await_args.args[0]
    assert [r.status for r in written] == ["OK"]
    assert result.stats.matched == 1
    assert result.stats.missing == 0


@pytest.mark.asyncio
async def test_new_register_overrides_old_on_key_clash() -> None:
    """When a number appears in both, the fresh export wins (corrected amount)."""
    onec, sheets = _services(
        onec_records=[_onec("U-1", amount=200.0)],            # corrected amount
        sheet_rows=[],
        existing_onec=[_onec("U-1", amount=100.0, source_row=2)],
    )
    await reconcile(
        raw=b"x", filename="r.xls", onec=onec, sheets=sheets, correlation_id="cid"
    )
    written = sheets.rewrite_reconciliation.await_args.args[0]
    u1_rows = [r for r in written if r.onec_upd_number == "U-1"]
    assert len(u1_rows) == 1
    assert u1_rows[0].onec_amount == 200.0


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
