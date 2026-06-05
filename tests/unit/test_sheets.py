"""Unit tests for SheetsService — gspread is mocked at module level."""

from __future__ import annotations

import json
from datetime import date
from unittest.mock import MagicMock

import pytest

import gspread

from app.core.errors import SheetsAppendError, SheetsReadError
from app.models import (
    OneCRecord,
    ReconciliationPlan,
    ReconRow,
    RowAnnotation,
    UPDRecord,
)
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
    assert len(values) == len(COLUMNS) == 13

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
    # Status column + file link.
    assert values[11] == ""                # L — Статус (filled on reconciliation)
    assert values[12] == ""                # M — Файл (empty when no Drive link)
    assert kwargs["value_input_option"] == "USER_ENTERED"


def test_append_row_writes_file_link_in_column_m(mock_worksheet):
    svc = SheetsService(credentials_json=VALID_CREDS, sheet_id="sheet-1")
    record = UPDRecord(
        organization="Гринлайн", date=date(2026, 4, 22), amount=1.0, upd_number="U-1"
    )
    svc.append_row_sync(
        record,
        foreman="Юра",
        correlation_id="cid",
        file_url="https://drive.google.com/file/d/abc/view",
    )
    values = mock_worksheet.append_row.call_args.args[0]
    assert len(values) == 13
    assert values[12] == "https://drive.google.com/file/d/abc/view"


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
            "https://drive/abc",  # M file link
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
    assert rows[0].file_url == "https://drive/abc"
    assert rows[0].source_row == 2
    assert rows[1].foreman == "Гриша"
    assert rows[1].status is None
    assert rows[1].file_url is None
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


def test_read_onec_records_reads_yellow_block(mock_worksheet):
    # Yellow block A..D is the 1С side left by the previous reconciliation.
    mock_worksheet.get_all_values.return_value = [
        list(COLUMNS),
        # OK row: yellow + green filled — yellow read.
        [
            "2026-04-22", "ООО Поставщик", "12345.67", "UPD-1",
            "UPD-1", "2026-04-22", "12345.67", 'ООО "Тест"', "Гринлайн", "Юра",
            "2026-04-22T10:00:00+00:00", "OK",
        ],
        # NO row: yellow only — yellow read.
        ["2026-04-23", "ООО Другой", "500", "UPD-3", "", "", "", "", "", "", "", "NO"],
        # ЛИШНЕЕ row: yellow empty → skipped.
        ["", "", "", "", "X-9", "2026-04-24", "50", "ООО", "", "Боря", "", "ЛИШНЕЕ"],
    ]
    svc = SheetsService(credentials_json=VALID_CREDS, sheet_id="sheet-1")
    rows = svc.read_onec_records_sync()

    assert [r.upd_number for r in rows] == ["UPD-1", "UPD-3"]
    assert rows[0].organization == "ООО Поставщик"
    assert rows[0].date == date(2026, 4, 22)
    assert rows[0].amount == 12345.67
    assert rows[0].source_row == 2
    assert rows[1].upd_number == "UPD-3"
    assert rows[1].amount == 500.0
    assert rows[1].source_row == 3


def test_read_onec_records_translates_api_error(mock_worksheet):
    fake_response = MagicMock()
    fake_response.status_code = 500
    fake_response.json.return_value = {"error": {"code": 500, "message": "boom"}}
    mock_worksheet.get_all_values.side_effect = gspread.exceptions.APIError(fake_response)

    svc = SheetsService(credentials_json=VALID_CREDS, sheet_id="sheet-1")
    with pytest.raises(SheetsReadError):
        svc.read_onec_records_sync()


def test_read_all_records_translates_api_error(mock_worksheet):
    fake_response = MagicMock()
    fake_response.status_code = 500
    fake_response.json.return_value = {"error": {"code": 500, "message": "boom"}}
    mock_worksheet.get_all_values.side_effect = gspread.exceptions.APIError(fake_response)

    svc = SheetsService(credentials_json=VALID_CREDS, sheet_id="sheet-1")
    with pytest.raises(SheetsReadError):
        svc.read_all_records_sync()


# --- annotate_reconciliation (non-destructive) ------------------------------


def test_annotate_reconciliation_patches_appends_deletes_and_formats(mock_worksheet):
    svc = SheetsService(credentials_json=VALID_CREDS, sheet_id="sheet-1")
    plan = ReconciliationPlan(
        annotations=[
            # Row 2: green upload matched → write yellow A:D + auto status.
            RowAnnotation(
                source_row=2,
                onec=OneCRecord(
                    upd_number="U-1",
                    date=date(2026, 4, 22),
                    amount=100.0,
                    organization="ООО Поставщик",
                    source_row=2,
                ),
                status="OK·авто",
            ),
            # Row 3: green-only ЛИШНЕЕ → status only, no yellow write.
            RowAnnotation(source_row=3, onec=None, status="ЛИШНЕЕ·авто"),
            # Row 4: manual status preserved → yellow written, status untouched.
            RowAnnotation(
                source_row=4,
                onec=OneCRecord(
                    upd_number="U-4",
                    date=date(2026, 4, 25),
                    amount=200.0,
                    organization="ООО Поставщик",
                    source_row=4,
                ),
                status=None,
            ),
        ],
        appended_rows=[
            ReconRow(
                status="NO·авто",
                onec_date=date(2026, 4, 23),
                onec_counterparty="ООО Другой",
                onec_amount=500.0,
                onec_upd_number="U-9",
            )
        ],
        deleted_rows=[5],
        last_data_row=5,
    )
    svc.annotate_reconciliation_sync(plan)

    # 1) Never clears the data area.
    mock_worksheet.batch_clear.assert_not_called()

    # 2) In-place patches: yellow A:D + status L, by row.
    mock_worksheet.batch_update.assert_called_once()
    updates = mock_worksheet.batch_update.call_args.args[0]
    by_range = {u["range"]: u["values"] for u in updates}
    assert by_range["A2:D2"][0][3] == "U-1"        # yellow upd written
    assert by_range["L2"][0][0] == "OK·авто"
    assert "A3:D3" not in by_range                  # ЛИШНЕЕ row has no yellow
    assert by_range["L3"][0][0] == "ЛИШНЕЕ·авто"
    assert "A4:D4" in by_range                       # manual row still gets yellow
    assert "L4" not in by_range                      # ...but status untouched

    # 3) Appended NO row written deterministically at A{last_data_row+1} = A6.
    mock_worksheet.update.assert_called_once()
    kwargs = mock_worksheet.update.call_args.kwargs
    assert kwargs["range_name"] == "A6"
    appended = kwargs["values"]
    assert len(appended) == 1 and len(appended[0]) == 13
    assert appended[0][3] == "U-9"        # D — № УПД (1С)
    assert appended[0][4] == ""           # E — green empty
    assert appended[0][11] == "NO·авто"   # L — status

    # 4) Deleted the stale placeholder row.
    mock_worksheet.delete_rows.assert_called_once_with(5)

    # 5) Repainted backgrounds + number formats. last_row = 5 + 1 - 1 = 5.
    # A range can appear twice (background AND numberFormat) so collect lists.
    mock_worksheet.batch_format.assert_called_once()
    formats = mock_worksheet.batch_format.call_args.args[0]

    def _fmts(rng):
        return [f["format"] for f in formats if f["range"] == rng]

    assert any("backgroundColor" in f for f in _fmts("A2:D5"))
    assert any("backgroundColor" in f for f in _fmts("E2:K5"))
    assert any("backgroundColor" in f for f in _fmts("M2:M5"))
    assert any(f.get("numberFormat", {}).get("type") == "DATE" for f in _fmts("A2:A5"))
    assert any(f.get("numberFormat", {}).get("type") == "NUMBER" for f in _fmts("G2:G5"))
    assert any(f.get("numberFormat", {}).get("type") == "TEXT" for f in _fmts("M2:M5"))


def test_annotate_reconciliation_appends_only_when_no_existing_data(mock_worksheet):
    """Empty sheet (header only): a fresh NO row lands at row 2."""
    svc = SheetsService(credentials_json=VALID_CREDS, sheet_id="sheet-1")
    plan = ReconciliationPlan(
        appended_rows=[ReconRow(status="NO·авто", onec_upd_number="U-1")],
        last_data_row=1,
    )
    svc.annotate_reconciliation_sync(plan)

    mock_worksheet.batch_update.assert_not_called()
    mock_worksheet.update.assert_called_once()
    assert mock_worksheet.update.call_args.kwargs["range_name"] == "A2"
    mock_worksheet.delete_rows.assert_not_called()
    mock_worksheet.batch_format.assert_called_once()


def test_annotate_reconciliation_noop_when_nothing_to_write(mock_worksheet):
    svc = SheetsService(credentials_json=VALID_CREDS, sheet_id="sheet-1")
    svc.annotate_reconciliation_sync(ReconciliationPlan(last_data_row=1))

    mock_worksheet.batch_clear.assert_not_called()
    mock_worksheet.batch_update.assert_not_called()
    mock_worksheet.update.assert_not_called()
    mock_worksheet.delete_rows.assert_not_called()
    mock_worksheet.batch_format.assert_not_called()


def test_annotate_reconciliation_translates_api_error(mock_worksheet):
    fake_response = MagicMock()
    fake_response.status_code = 503
    fake_response.json.return_value = {"error": {"code": 503, "message": "down"}}
    mock_worksheet.batch_update.side_effect = gspread.exceptions.APIError(fake_response)

    svc = SheetsService(credentials_json=VALID_CREDS, sheet_id="sheet-1")
    with pytest.raises(SheetsAppendError):
        svc.annotate_reconciliation_sync(
            ReconciliationPlan(
                annotations=[RowAnnotation(source_row=2, status="NO·авто")],
                last_data_row=2,
            )
        )
