"""Unit tests for OneCParserService — covers .xls (real fixture), .xlsx, .csv."""

from __future__ import annotations

import csv
import io
from datetime import date
from pathlib import Path

import openpyxl
import pytest

from app.core.errors import OneCParseError
from app.services.onec import OneCParserService


FIXTURES_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "onec"


@pytest.fixture
def parser() -> OneCParserService:
    return OneCParserService()


def _real_xls_bytes() -> bytes:
    return (FIXTURES_DIR / "sample.xls").read_bytes()


# ---------------------------------------------------------------------------
# .xls (real 1С fixture)
# ---------------------------------------------------------------------------


def test_parse_real_xls_extracts_records(parser: OneCParserService) -> None:
    records = parser.parse(_real_xls_bytes(), "sample.xls")
    assert len(records) == 39  # known from fixture inspection
    first = records[0]
    assert first.upd_number == "6022461056"
    assert first.date == date(2026, 2, 5)
    assert first.amount == pytest.approx(21324.0)
    assert first.organization == "СТРОИТЕЛЬНЫЙ ДВОР ООО"
    assert first.source_row == 7  # 1-based, header is row 6


def test_parse_real_xls_stops_at_total(parser: OneCParserService) -> None:
    records = parser.parse(_real_xls_bytes(), "sample.xls")
    assert all(r.upd_number for r in records)
    # No record should reflect the signature block at the bottom of the file
    last = records[-1]
    assert last.upd_number == "6022618882"


# ---------------------------------------------------------------------------
# .xlsx (synthetic)
# ---------------------------------------------------------------------------


def _build_xlsx(rows: list[list[object]]) -> bytes:
    wb = openpyxl.Workbook()
    ws = wb.active
    for row in rows:
        ws.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def test_parse_xlsx_with_synthetic_sheet(parser: OneCParserService) -> None:
    rows = [
        ["ИП Тест", "", "", "", "", "", "", ""],
        ["Реестр", "", "", "", "", "", "", ""],
        ["№ п/п", "Дата", "Документ", "Номер", "Дата вх.", "Номер вх.", "Сумма", "Информация"],
        [1, date(2026, 1, 10), "Накладная", "1", date(2026, 1, 10), "U-100", 1500.5, "ООО Ромашка"],
        [2, date(2026, 1, 11), "Накладная", "2", date(2026, 1, 11), "U-101", 2000, "ООО Ромашка"],
        ["Итого", "", "", "", "", "", 3500.5, ""],
    ]
    records = parser.parse(_build_xlsx(rows), "sample.xlsx")
    assert len(records) == 2
    assert records[0].upd_number == "U-100"
    assert records[0].date == date(2026, 1, 10)
    assert records[0].amount == pytest.approx(1500.5)
    assert records[1].upd_number == "U-101"
    assert records[1].amount == pytest.approx(2000)


# ---------------------------------------------------------------------------
# .csv
# ---------------------------------------------------------------------------


def _build_csv(rows: list[list[str]], *, delim: str = ";", bom: bool = False) -> bytes:
    buf = io.StringIO()
    writer = csv.writer(buf, delimiter=delim)
    for row in rows:
        writer.writerow(row)
    text = buf.getvalue()
    if bom:
        text = "﻿" + text
    return text.encode("utf-8")


def test_parse_csv_semicolon(parser: OneCParserService) -> None:
    rows = [
        ["Меta", "", "", "", "", ""],
        ["№ п/п", "Дата", "Дата вх.", "Номер вх.", "Сумма", "Информация"],
        ["1", "10.01.2026", "10.01.2026", "C-1", "1234,56", "ООО Альфа"],
        ["2", "11.01.2026", "11.01.2026", "C-2", "2000", "ООО Альфа"],
    ]
    data = _build_csv(rows)
    records = parser.parse(data, "register.csv")
    assert [r.upd_number for r in records] == ["C-1", "C-2"]
    assert records[0].amount == pytest.approx(1234.56)
    assert records[0].date == date(2026, 1, 10)


def test_parse_csv_with_bom(parser: OneCParserService) -> None:
    rows = [
        ["№ п/п", "Дата", "Дата вх.", "Номер вх.", "Сумма", "Информация"],
        ["1", "10.01.2026", "10.01.2026", "BOM-1", "100", "ООО"],
    ]
    data = _build_csv(rows, bom=True)
    records = parser.parse(data, "bom.csv")
    assert len(records) == 1
    assert records[0].upd_number == "BOM-1"


# ---------------------------------------------------------------------------
# error paths
# ---------------------------------------------------------------------------


def test_parse_missing_required_columns(parser: OneCParserService) -> None:
    rows = [
        ["№", "Дата", "Документ"],  # no «Номер» / «Сумма»
        [1, "10.01.2026", "Накладная"],
    ]
    with pytest.raises(OneCParseError):
        parser.parse(_build_xlsx(rows), "broken.xlsx")


def test_parse_empty_file_raises(parser: OneCParserService) -> None:
    with pytest.raises(OneCParseError):
        parser.parse(b"", "empty.csv")


def test_parse_unsupported_extension_raises(parser: OneCParserService) -> None:
    with pytest.raises(OneCParseError):
        parser.parse(b"%PDF-1.4\nfake", "report.pdf")


def test_parse_skips_rows_without_upd_number(parser: OneCParserService) -> None:
    rows = [
        ["№ п/п", "Дата", "Дата вх.", "Номер вх.", "Сумма", "Информация"],
        ["1", "10.01.2026", "10.01.2026", "", "100", "ООО"],  # skipped — empty upd
        ["2", "11.01.2026", "11.01.2026", "OK-1", "200", "ООО"],
    ]
    data = _build_csv(rows)
    records = parser.parse(data, "skip.csv")
    assert [r.upd_number for r in records] == ["OK-1"]
