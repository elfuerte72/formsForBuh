"""Unit tests for SheetsService — gspread is mocked at module level."""

from __future__ import annotations

import json
from datetime import date
from unittest.mock import MagicMock

import pytest

import gspread

from app.core.errors import SheetsAppendError, SheetsReadError
from app.models import ReconRow, UPDRecord
from app.services import sheets as sheets_module
from app.services.sheets import COLUMNS, SheetsService


VALID_CREDS = json.dumps(
    {
        "type": "service_account",
        "project_id": "p",
        "private_key_id": "k",
        "private_key": "-----BEGIN PRIVATE KEY-----\nfake\n-----END PRIVATE KEY-----\n",
        "client_email": "x@x.iam.gserviceaccount.com",
        "client_id": "1",
        "token_uri": "https://oauth2.googleapis.com/token",
    }
)


@pytest.fixture
def mock_worksheet(monkeypatch):
    """Patch gspread.authorize so SheetsService never touches the network."""
    worksheet = MagicMock(name="worksheet")
    spreadsheet = MagicMock(name="spreadsheet")
    spreadsheet.sheet1 = worksheet

    client = MagicMock(name="client")
    client.open_by_key.return_value = spreadsheet

    monkeypatch.setattr(sheets_module.gspread, "authorize", lambda creds: client)
    monkeypatch.setattr(
        sheets_module.Credentials, "from_service_account_info", lambda info, scopes: object()
    )
    return worksheet


def test_append_row_writes_green_block_and_leaves_yellow_empty(mock_worksheet):
    svc = SheetsService(credentials_json=VALID_CREDS, sheet_id="sheet-1")
    record = UPDRecord(
        organization="Гринлайн",
        counterparty='ООО "Тест"',
        date=date(2026, 4, 22),
        amount=12345.67,
        upd_number="UPD-1",
    )
    svc.append_row_sync(record, foreman="Юра", correlation_id="cid-abc")

    mock_worksheet.append_row.assert_called_once()
    args, kwargs = mock_worksheet.append_row.call_args
    values = args[0]
    assert len(values) == len(COLUMNS) == 12

    # Yellow block (A..D) — empty on foreman upload.
    assert values[0] == "" and values[1] == "" and values[2] == "" and values[3] == ""
    # Green block (E..K).
    assert values[4] == "UPD-1"           # E — № УПД
    assert values[5] == "2026-04-22"       # F — Дата
    assert values[6] == 12345.67           # G — Сумма
    assert values[7] == 'ООО "Тест"'       # H — Контрагент
    assert values[8] == "Гринлайн"         # I — Организация
    assert values[9] == "Юра"              # J — Прораб
    assert isinstance(values[10], str) and "T" in values[10]  # K — ISO timestamp
    # Status column.
    assert values[11] == ""                # L — Статус (filled on reconciliation)
    assert kwargs["value_input_option"] == "USER_ENTERED"


def test_append_row_handles_missing_fields(mock_worksheet):
    svc = SheetsService(credentials_json=VALID_CREDS, sheet_id="sheet-1")
    record = UPDRecord(
        organization=None,
        counterparty=None,
        date=None,
        amount=None,
        upd_number=None,
    )
    svc.append_row_sync(record, foreman="Боря", correlation_id="cid")

    values = mock_worksheet.append_row.call_args.args[0]
    # Green block stays empty when all fields are missing.
    assert values[4] == "" and values[5] == "" and values[6] == ""
    assert values[7] == "" and values[8] == ""
    assert values[9] == "Боря"  # foreman is still set


def test_append_row_translates_api_error(mock_worksheet):
    fake_response = MagicMock()
    fake_response.status_code = 503
    fake_response.json.return_value = {"error": {"code": 503, "message": "down"}}
    mock_worksheet.append_row.side_effect = gspread.exceptions.APIError(fake_response)
    svc = SheetsService(credentials_json=VALID_CREDS, sheet_id="sheet-1")
    record = UPDRecord(
        organization="X", date=date(2026, 1, 1), amount=1.0, upd_number="1"
    )
    with pytest.raises(SheetsAppendError):
        svc.append_row_sync(record, foreman="Юра", correlation_id="cid")


def test_invalid_credentials_json_raises(monkeypatch):
    svc = SheetsService(credentials_json="not-json", sheet_id="sheet-1")
    record = UPDRecord(
        organization="X", date=date(2026, 1, 1), amount=1.0, upd_number="1"
    )
    with pytest.raises(SheetsAppendError):
        svc.append_row_sync(record, foreman="Юра", correlation_id="cid")


def test_read_all_records_reads_green_block(mock_worksheet):
    # 12-column layout: yellow A..D, green E..K, status L.
    mock_worksheet.get_all_values.return_value = [
        list(COLUMNS),
        # Row 2: full OK row left by previous reconciliation.
        [
            "2026-04-22",         # A yellow date
            "ООО Поставщик",      # B yellow counterparty
            "12345.67",           # C yellow amount
            "UPD-1",              # D yellow upd
            "UPD-1",              # E green upd
            "2026-04-22",         # F green date
            "12345.67",           # G green amount
            'ООО "Тест"',         # H green counterparty
            "Гринлайн",           # I green organization
            "Юра",                # J foreman
            "2026-04-22T10:00:00+00:00",  # K uploaded_at
            "OK",                 # L status
        ],
        # Row 3: NO row from previous reconciliation — yellow only, green empty.
        [
            "2026-04-23",
            "ООО Поставщик",
            "500",
            "UPD-3",
            "",  # E empty → skipped
            "",
            "",
            "",
            "",
            "",
            "",
            "NO",
        ],
        # Row 4: fresh foreman upload — green only, status empty.
        [
            "", "", "", "",
            "UPD-2",
            "2026-04-23",
            "200",
            "ООО Тест",
            "Гринлайн",
            "Гриша",
            "",
            "",
        ],
    ]
    svc = SheetsService(credentials_json=VALID_CREDS, sheet_id="sheet-1")
    rows = svc.read_all_records_sync()

    assert [r.upd_number for r in rows] == ["UPD-1", "UPD-2"]
    assert rows[0].organization == "Гринлайн"
    assert rows[0].counterparty == 'ООО "Тест"'
    assert rows[0].date == date(2026, 4, 22)
    assert rows[0].amount == 12345.67
    assert rows[0].foreman == "Юра"
    assert rows[0].status == "OK"
    assert rows[0].source_row == 2
    assert rows[1].foreman == "Гриша"
    assert rows[1].status is None
    assert rows[1].source_row == 4


def test_read_all_records_skips_rows_without_green_upd(mock_worksheet):
    mock_worksheet.get_all_values.return_value = [
        list(COLUMNS),
        # NO row: yellow filled, green empty — skipped.
        ["2026-04-22", "ООО", "100", "U-1", "", "", "", "", "", "", "", "NO"],
        # Green-only row — kept.
        ["", "", "", "", "U-2", "2026-04-22", "100", "ООО", "", "Юра", "", ""],
    ]
    svc = SheetsService(credentials_json=VALID_CREDS, sheet_id="sheet-1")
    rows = svc.read_all_records_sync()
    assert [r.upd_number for r in rows] == ["U-2"]


def test_read_all_records_translates_api_error(mock_worksheet):
    fake_response = MagicMock()
    fake_response.status_code = 500
    fake_response.json.return_value = {"error": {"code": 500, "message": "boom"}}
    mock_worksheet.get_all_values.side_effect = gspread.exceptions.APIError(fake_response)

    svc = SheetsService(credentials_json=VALID_CREDS, sheet_id="sheet-1")
    with pytest.raises(SheetsReadError):
        svc.read_all_records_sync()


# --- rewrite_reconciliation -------------------------------------------------


def test_rewrite_reconciliation_clears_writes_and_formats(mock_worksheet):
    svc = SheetsService(credentials_json=VALID_CREDS, sheet_id="sheet-1")
    rows = [
        ReconRow(
            status="OK",
            onec_date=date(2026, 4, 22),
            onec_counterparty="ООО Поставщик",
            onec_amount=100.0,
            onec_upd_number="U-1",
            green_upd_number="U-1",
            green_date=date(2026, 4, 22),
            green_amount=100.0,
            green_counterparty="ООО Тест",
            green_organization="Гринлайн",
            green_foreman="Юра",
            green_uploaded_at="2026-04-22T10:00:00+00:00",
        ),
        ReconRow(
            status="NO",
            onec_date=date(2026, 4, 23),
            onec_counterparty="ООО Другой",
            onec_amount=500.0,
            onec_upd_number="U-3",
        ),
        ReconRow(
            status="ЛИШНЕЕ",
            green_upd_number="X-9",
            green_date=date(2026, 4, 24),
            green_amount=50.0,
            green_counterparty="ООО Где-то",
            green_organization="Гринлайн",
            green_foreman="Боря",
            green_uploaded_at="2026-04-24T10:00:00+00:00",
        ),
    ]
    svc.rewrite_reconciliation_sync(rows)

    # 1) Cleared the data area below the header.
    mock_worksheet.batch_clear.assert_called_once_with(["A2:L"])

    # 2) Wrote all rows in one batch starting at A2.
    mock_worksheet.update.assert_called_once()
    kwargs = mock_worksheet.update.call_args.kwargs
    assert kwargs["range_name"] == "A2"
    assert kwargs["value_input_option"] == "USER_ENTERED"
    values = kwargs["values"]
    assert len(values) == 3
    assert len(values[0]) == 12
    # Row 0: OK — yellow + green filled, status L=OK.
    assert values[0][0] == "2026-04-22"
    assert values[0][3] == "U-1"
    assert values[0][4] == "U-1"
    assert values[0][11] == "OK"
    # Row 1: NO — green columns empty.
    assert values[1][4] == ""
    assert values[1][11] == "NO"
    # Row 2: ЛИШНЕЕ — yellow columns empty.
    assert values[2][0] == "" and values[2][3] == ""
    assert values[2][4] == "X-9"
    assert values[2][11] == "ЛИШНЕЕ"

    # 3) Painted yellow + green backgrounds AND number formats in one batch.
    # Last data row = 3 rows + header row 1 = 4.
    mock_worksheet.batch_format.assert_called_once()
    formats = mock_worksheet.batch_format.call_args.args[0]
    by_range = {item["range"]: item["format"] for item in formats}
    assert "backgroundColor" in by_range["A2:D4"]
    assert "backgroundColor" in by_range["E2:K4"]
    # Date columns get DATE format; amount columns get NUMBER; upd numbers stay TEXT.
    assert by_range["A2:A4"]["numberFormat"]["type"] == "DATE"
    assert by_range["C2:C4"]["numberFormat"]["type"] == "NUMBER"
    assert by_range["D2:D4"]["numberFormat"]["type"] == "TEXT"
    assert by_range["F2:F4"]["numberFormat"]["type"] == "DATE"
    assert by_range["G2:G4"]["numberFormat"]["type"] == "NUMBER"


def test_rewrite_reconciliation_empty_clears_only(mock_worksheet):
    svc = SheetsService(credentials_json=VALID_CREDS, sheet_id="sheet-1")
    svc.rewrite_reconciliation_sync([])

    mock_worksheet.batch_clear.assert_called_once_with(["A2:L"])
    mock_worksheet.update.assert_not_called()
    mock_worksheet.batch_format.assert_not_called()


def test_rewrite_reconciliation_translates_api_error(mock_worksheet):
    fake_response = MagicMock()
    fake_response.status_code = 503
    fake_response.json.return_value = {"error": {"code": 503, "message": "down"}}
    mock_worksheet.batch_clear.side_effect = gspread.exceptions.APIError(fake_response)

    svc = SheetsService(credentials_json=VALID_CREDS, sheet_id="sheet-1")
    with pytest.raises(SheetsAppendError):
        svc.rewrite_reconciliation_sync(
            [ReconRow(status="NO", onec_upd_number="U-1")]
        )
