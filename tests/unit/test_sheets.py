"""Unit tests for SheetsService — gspread is mocked at module level."""

from __future__ import annotations

import json
from datetime import date
from unittest.mock import MagicMock

import pytest

import gspread

from app.core.errors import SheetsAppendError, SheetsReadError
from app.models import UPDRecord
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


def test_append_row_uses_correct_column_order(mock_worksheet):
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
    assert len(values) == len(COLUMNS)
    assert values[0] == "Гринлайн"
    assert values[1] == 'ООО "Тест"'
    assert values[2] == "2026-04-22"
    assert values[3] == 12345.67
    assert values[4] == "UPD-1"
    assert values[5] == "Юра"
    assert isinstance(values[6], str) and "T" in values[6]  # ISO timestamp
    assert values[7] == ""  # «Статус» filled by hand
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
    assert values[0] == ""
    assert values[1] == ""
    assert values[2] == ""
    assert values[3] == ""
    assert values[4] == ""


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


def test_read_all_records_happy_path(mock_worksheet):
    mock_worksheet.get_all_values.return_value = [
        list(COLUMNS),
        [
            "Гринлайн",
            'ООО "Тест"',
            "2026-04-22",
            "12345.67",
            "UPD-1",
            "Юра",
            "2026-04-22T10:00:00+00:00",
            "Принят оригинал",
        ],
        [
            "",
            "",
            "2026-04-23",
            "200",
            "UPD-2",
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
    assert rows[0].status == "Принят оригинал"
    assert rows[0].source_row == 2
    assert rows[1].organization is None  # blank cell becomes None
    assert rows[1].counterparty is None
    assert rows[1].status is None
    assert rows[1].source_row == 3


def test_read_all_records_skips_rows_without_upd(mock_worksheet):
    mock_worksheet.get_all_values.return_value = [
        list(COLUMNS),
        ["", "", "", "", "", "Юра", "", ""],  # no upd_number — skip
        ["", "", "2026-04-22", "100", "U-1", "Юра", "", ""],
    ]
    svc = SheetsService(credentials_json=VALID_CREDS, sheet_id="sheet-1")
    rows = svc.read_all_records_sync()
    assert [r.upd_number for r in rows] == ["U-1"]


def test_read_all_records_translates_api_error(mock_worksheet):
    fake_response = MagicMock()
    fake_response.status_code = 500
    fake_response.json.return_value = {"error": {"code": 500, "message": "boom"}}
    mock_worksheet.get_all_values.side_effect = gspread.exceptions.APIError(fake_response)

    svc = SheetsService(credentials_json=VALID_CREDS, sheet_id="sheet-1")
    with pytest.raises(SheetsReadError):
        svc.read_all_records_sync()
