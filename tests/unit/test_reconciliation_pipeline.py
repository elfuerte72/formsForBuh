"""Unit tests for the reconciliation pipeline (diff + non-destructive write).

The pipeline now produces a :class:`ReconciliationPlan` (in-place annotations +
appended NO rows + deleted stale placeholders) and hands it to
``sheets.annotate_reconciliation`` — nothing is cleared or reordered. Statuses
written by reconciliation carry the ``·авто`` marker so a bookkeeper's manual
status survives the next run.
"""

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
    annotate_exc=None,
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
    if annotate_exc is not None:
        sheets.annotate_reconciliation = AsyncMock(side_effect=annotate_exc)
    else:
        sheets.annotate_reconciliation = AsyncMock(return_value=None)

    return onec, sheets


def _plan(sheets):
    """The ReconciliationPlan handed to annotate_reconciliation."""
    return sheets.annotate_reconciliation.await_args.args[0]


def _statuses(plan) -> list[str]:
    """Every status the plan writes — annotated (non-None) + appended."""
    return [a.status for a in plan.annotations if a.status is not None] + [
        r.status for r in plan.appended_rows
    ]


def _no_numbers(plan) -> set[str]:
    """1С numbers landing as NO rows — refreshed-in-place + appended."""
    return {
        a.onec.upd_number
        for a in plan.annotations
        if a.status == "NO·авто" and a.onec is not None
    } | {r.onec_upd_number for r in plan.appended_rows}


@pytest.mark.asyncio
async def test_perfect_match_writes_ok_rows() -> None:
    onec, sheets = _services(
        onec_records=[_onec("U-1"), _onec("U-2")],
        sheet_rows=[_sheet("U-1", source_row=2), _sheet("U-2", source_row=3)],
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

    sheets.annotate_reconciliation.assert_awaited_once()
    plan = _plan(sheets)
    assert _statuses(plan) == ["OK·авто", "OK·авто"]
    assert not plan.appended_rows
    assert not plan.deleted_rows
    # Each matched green row is patched in place with its 1С block.
    assert all(a.onec is not None for a in plan.annotations)
    assert {a.source_row for a in plan.annotations} == {2, 3}


@pytest.mark.asyncio
async def test_missing_emits_no_row_and_counts_in_stats() -> None:
    onec, sheets = _services(
        onec_records=[_onec("U-1"), _onec("U-2", source_row=11)],
        sheet_rows=[_sheet("U-2", source_row=2)],
    )
    result = await reconcile(
        raw=b"x", filename="r.xls", onec=onec, sheets=sheets, correlation_id="cid"
    )
    assert result.ok is True
    assert result.stats.matched == 1
    assert result.stats.missing == 1
    assert result.stats.extras == 0

    plan = _plan(sheets)
    # U-2 matched (annotation), U-1 missing → appended NO row.
    assert len(plan.appended_rows) == 1
    no_row = plan.appended_rows[0]
    assert no_row.status == "NO·авто"
    assert no_row.onec_upd_number == "U-1"
    assert no_row.green_upd_number is None
    assert [a.status for a in plan.annotations] == ["OK·авто"]


@pytest.mark.asyncio
async def test_extras_emit_lishnee_annotation() -> None:
    onec, sheets = _services(
        onec_records=[_onec("U-1")],
        sheet_rows=[
            _sheet("U-1", source_row=2),
            _sheet("X-9", foreman="Боря", source_row=3),
        ],
    )
    result = await reconcile(
        raw=b"x", filename="r.xls", onec=onec, sheets=sheets, correlation_id="cid"
    )
    assert result.stats.matched == 1
    assert result.stats.extras == 1
    assert result.stats.missing == 0

    plan = _plan(sheets)
    extra = next(a for a in plan.annotations if a.status == "ЛИШНЕЕ·авто")
    assert extra.source_row == 3
    assert extra.onec is None  # no 1С match → yellow left empty
    assert not plan.appended_rows


@pytest.mark.asyncio
async def test_foreman_duplicates_produce_multiple_ok_annotations() -> None:
    """Two foreman uploads of the same UPD stay as two paired OK rows."""
    onec, sheets = _services(
        onec_records=[_onec("U-1")],
        sheet_rows=[
            _sheet("U-1", foreman="Юра", source_row=2),
            _sheet("U-1", foreman="Гриша", source_row=3),
        ],
    )
    result = await reconcile(
        raw=b"x", filename="r.xls", onec=onec, sheets=sheets, correlation_id="cid"
    )
    plan = _plan(sheets)
    ok_rows = [a for a in plan.annotations if a.status == "OK·авто"]
    assert len(ok_rows) == 2
    assert {a.source_row for a in ok_rows} == {2, 3}
    assert result.stats.matched == 2


@pytest.mark.asyncio
async def test_amount_mismatch_flags_summa_status_not_ok() -> None:
    """Same number, different amount → СУММА? annotation + amount_mismatch count."""
    onec, sheets = _services(
        onec_records=[_onec("U-1", amount=15888.0)],
        sheet_rows=[_sheet("U-1", amount=15348.0, source_row=2)],
    )
    result = await reconcile(
        raw=b"x", filename="r.xls", onec=onec, sheets=sheets, correlation_id="cid"
    )
    assert result.stats.matched == 0
    assert result.stats.amount_mismatch == 1
    assert result.stats.missing == 0
    assert result.stats.extras == 0

    plan = _plan(sheets)
    ann = next(a for a in plan.annotations if a.status == "СУММА?·авто")
    assert ann.onec.amount == 15888.0
    assert ann.onec.upd_number == "U-1"


@pytest.mark.asyncio
async def test_amount_within_tolerance_stays_ok() -> None:
    onec, sheets = _services(
        onec_records=[_onec("U-1", amount=100.00)],
        sheet_rows=[_sheet("U-1", amount=100.009, source_row=2)],  # < 0.01 gap
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
        sheet_rows=[_sheet("U-1", amount=500.0, source_row=2)],
    )
    result = await reconcile(
        raw=b"x", filename="r.xls", onec=onec, sheets=sheets, correlation_id="cid"
    )
    assert result.stats.matched == 1
    assert result.stats.amount_mismatch == 0


@pytest.mark.asyncio
async def test_manual_status_is_preserved() -> None:
    """A status the bookkeeper set by hand (no marker) is never overwritten."""
    onec, sheets = _services(
        onec_records=[_onec("U-1")],
        sheet_rows=[_sheet("U-1", status="OK", source_row=2)],  # manual, no marker
    )
    result = await reconcile(
        raw=b"x", filename="r.xls", onec=onec, sheets=sheets, correlation_id="cid"
    )
    assert result.stats.matched == 1
    plan = _plan(sheets)
    ann = plan.annotations[0]
    # Yellow is still written, but the status cell is left untouched (None).
    assert ann.onec is not None
    assert ann.status is None


@pytest.mark.asyncio
async def test_auto_status_is_refreshed() -> None:
    """A previous auto-status (with marker) is recomputed, e.g. ЛИШНЕЕ → OK."""
    onec, sheets = _services(
        onec_records=[_onec("U-1")],
        sheet_rows=[_sheet("U-1", status="ЛИШНЕЕ·авто", source_row=2)],
    )
    await reconcile(
        raw=b"x", filename="r.xls", onec=onec, sheets=sheets, correlation_id="cid"
    )
    plan = _plan(sheets)
    assert plan.annotations[0].status == "OK·авто"


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
        sheet_rows=[_sheet(" 12 345 ", source_row=2)],
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
    plan = _plan(sheets)
    assert _no_numbers(plan) == {"U-1", "U-2"}
    assert all(s == "NO·авто" for s in _statuses(plan))
    assert not plan.deleted_rows
    assert result.stats.missing == 2


@pytest.mark.asyncio
async def test_carried_over_register_record_matches_fresh_upload() -> None:
    """A foreman upload pairs with a register row carried over from last week,
    and the stale yellow-only placeholder for it is removed."""
    onec, sheets = _services(
        onec_records=[],                               # nothing in the new export
        sheet_rows=[_sheet("U-1", source_row=2)],      # foreman uploaded U-1 now
        existing_onec=[_onec("U-1", source_row=5)],    # U-1 placeholder from before
    )
    result = await reconcile(
        raw=b"x", filename="r.xls", onec=onec, sheets=sheets, correlation_id="cid"
    )
    plan = _plan(sheets)
    assert [a.status for a in plan.annotations] == ["OK·авто"]
    assert plan.annotations[0].source_row == 2
    assert plan.deleted_rows == [5]  # placeholder removed → no duplicate
    assert result.stats.matched == 1
    assert result.stats.missing == 0


@pytest.mark.asyncio
async def test_orphan_no_placeholder_deleted_when_uploaded() -> None:
    """The core workflow: a NO row becomes OK once the foreman uploads it.

    The 1С record had a yellow-only NO placeholder; a later upload now matches
    it, so the placeholder is deleted and the green row becomes the OK row.
    """
    onec, sheets = _services(
        onec_records=[_onec("U-1")],
        sheet_rows=[_sheet("U-1", source_row=3)],       # fresh green upload
        existing_onec=[_onec("U-1", source_row=2)],     # old yellow-only NO row
    )
    result = await reconcile(
        raw=b"x", filename="r.xls", onec=onec, sheets=sheets, correlation_id="cid"
    )
    plan = _plan(sheets)
    assert plan.deleted_rows == [2]
    ok = [a for a in plan.annotations if a.status == "OK·авто"]
    assert len(ok) == 1 and ok[0].source_row == 3
    assert result.stats.matched == 1
    assert result.stats.missing == 0


@pytest.mark.asyncio
async def test_side_by_side_row_is_refreshed_not_deleted() -> None:
    """A type-b row (yellow+green on the SAME physical row) is never deleted."""
    onec, sheets = _services(
        onec_records=[_onec("U-1", amount=100.0)],
        sheet_rows=[_sheet("U-1", amount=100.0, source_row=2)],
        existing_onec=[_onec("U-1", amount=100.0, source_row=2)],  # same row → type-b
    )
    await reconcile(
        raw=b"x", filename="r.xls", onec=onec, sheets=sheets, correlation_id="cid"
    )
    plan = _plan(sheets)
    assert plan.deleted_rows == []
    assert plan.annotations[0].source_row == 2
    assert plan.annotations[0].status == "OK·авто"


@pytest.mark.asyncio
async def test_new_register_overrides_old_on_key_clash() -> None:
    """When a number appears in both, the fresh export wins (corrected amount)."""
    onec, sheets = _services(
        onec_records=[_onec("U-1", amount=200.0)],            # corrected amount
        sheet_rows=[],
        existing_onec=[_onec("U-1", amount=100.0, source_row=5)],
    )
    await reconcile(
        raw=b"x", filename="r.xls", onec=onec, sheets=sheets, correlation_id="cid"
    )
    plan = _plan(sheets)
    # U-1 has a yellow-only placeholder (row 5) → refreshed in place, new amount.
    refreshed = [a for a in plan.annotations if a.onec and a.onec.upd_number == "U-1"]
    assert len(refreshed) == 1
    assert refreshed[0].onec.amount == 200.0
    assert refreshed[0].source_row == 5


@pytest.mark.asyncio
async def test_idempotent_second_run_changes_nothing_structural() -> None:
    """Re-running the same export over an already-reconciled sheet is stable:
    no rows appended, none deleted, statuses stay OK."""
    onec, sheets = _services(
        onec_records=[_onec("U-1"), _onec("U-2")],
        sheet_rows=[
            _sheet("U-1", status="OK·авто", source_row=2),
            _sheet("U-2", status="OK·авто", source_row=3),
        ],
        existing_onec=[
            _onec("U-1", source_row=2),
            _onec("U-2", source_row=3),
        ],
    )
    result = await reconcile(
        raw=b"x", filename="r.xls", onec=onec, sheets=sheets, correlation_id="cid"
    )
    plan = _plan(sheets)
    assert not plan.appended_rows
    assert not plan.deleted_rows
    assert [a.status for a in plan.annotations] == ["OK·авто", "OK·авто"]
    assert result.stats.matched == 2
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
    sheets.annotate_reconciliation.assert_not_awaited()


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
    sheets.annotate_reconciliation.assert_not_awaited()


@pytest.mark.asyncio
async def test_sheets_write_error_returned_as_machine_code() -> None:
    onec, sheets = _services(
        onec_records=[_onec("U-1")],
        sheet_rows=[_sheet("U-1", source_row=2)],
        annotate_exc=SheetsAppendError("api down"),
    )
    result = await reconcile(
        raw=b"x", filename="r.xls", onec=onec, sheets=sheets, correlation_id="cid"
    )
    assert result.ok is False
    assert result.error == "sheets_write_error"
