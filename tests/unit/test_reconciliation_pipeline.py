"""Unit tests for the reconciliation pipeline (diff + error translation)."""

from __future__ import annotations

from datetime import date
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.core.errors import OneCParseError, SheetsReadError
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


def _services(*, onec_records=None, sheet_rows=None,
              onec_exc=None, sheets_exc=None):
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

    return onec, sheets


@pytest.mark.asyncio
async def test_perfect_match_yields_no_diff() -> None:
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
    assert result.missing == []
    assert result.duplicates == []
    assert result.extras == []
    assert result.stats.matched == 2
    assert result.stats.coverage_percent == 100.0


@pytest.mark.asyncio
async def test_missing_lists_unmatched_onec_rows() -> None:
    onec, sheets = _services(
        onec_records=[_onec("U-1"), _onec("U-2", source_row=11)],
        sheet_rows=[_sheet("U-2")],
    )
    result = await reconcile(
        raw=b"x", filename="r.xls", onec=onec, sheets=sheets, correlation_id="cid"
    )
    assert [m.upd_number for m in result.missing] == ["U-1"]
    assert result.stats.missing == 1
    assert result.stats.matched == 1
    assert result.stats.coverage_percent == 50.0


@pytest.mark.asyncio
async def test_duplicates_collected_when_sheet_has_repeats() -> None:
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
    assert len(result.duplicates) == 1
    dup = result.duplicates[0]
    assert dup.upd_number == "U-1"
    assert dup.count == 2
    assert sorted(dup.foremen) == ["Гриша", "Юра"]


@pytest.mark.asyncio
async def test_extras_listed_when_sheet_has_unknown_upd() -> None:
    onec, sheets = _services(
        onec_records=[_onec("U-1")],
        sheet_rows=[_sheet("U-1"), _sheet("X-9", foreman="Боря")],
    )
    result = await reconcile(
        raw=b"x", filename="r.xls", onec=onec, sheets=sheets, correlation_id="cid"
    )
    assert [e.upd_number for e in result.extras] == ["X-9"]
    assert result.extras[0].foreman == "Боря"


@pytest.mark.asyncio
async def test_normalization_matches_padding_and_spaces() -> None:
    """A 1С number `00012345` and a sheet number ` 12 345 ` must match."""
    onec, sheets = _services(
        onec_records=[_onec("00012345")],
        sheet_rows=[_sheet(" 12 345 ")],
    )
    result = await reconcile(
        raw=b"x", filename="r.xls", onec=onec, sheets=sheets, correlation_id="cid"
    )
    assert result.missing == []
    assert result.extras == []
    assert result.stats.matched == 1


@pytest.mark.asyncio
async def test_onec_parse_error_returned_as_machine_code() -> None:
    onec, sheets = _services(onec_exc=OneCParseError("broken"))
    result = await reconcile(
        raw=b"x", filename="r.xls", onec=onec, sheets=sheets, correlation_id="cid"
    )
    assert result.ok is False
    assert result.error == "onec_parse_error"
    sheets.read_all_records.assert_not_awaited()


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
