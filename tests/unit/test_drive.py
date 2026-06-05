"""Unit tests for DriveService — googleapiclient + OAuth are mocked."""

from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock

import pytest

from google.auth.exceptions import RefreshError
from googleapiclient.errors import HttpError

from app.core.errors import DriveUploadError
from app.services import drive as drive_module
from app.services.drive import DriveService


@pytest.fixture
def mock_files(monkeypatch):
    """Patch build()/UserCredentials so DriveService never touches the network.

    Returns the mocked ``service.files()`` handle; its ``.list()`` and
    ``.create()`` chains end in ``.execute()`` so tests can script responses.
    """
    files = MagicMock(name="files")
    service = MagicMock(name="service")
    service.files.return_value = files

    monkeypatch.setattr(drive_module, "build", lambda *a, **k: service)
    monkeypatch.setattr(
        drive_module, "UserCredentials", lambda *a, **k: object()
    )
    return files


def _svc() -> DriveService:
    return DriveService(
        client_id="cid",
        client_secret="csecret",
        refresh_token="rtoken",
        parent_folder_id="parent-1",
    )


def test_upload_creates_day_folder_and_returns_link(mock_files):
    # Folder for the day does not exist yet → it gets created, then the file.
    mock_files.list.return_value.execute.return_value = {"files": []}
    mock_files.create.return_value.execute.side_effect = [
        {"id": "folder-1"},
        {"id": "file-1", "webViewLink": "https://drive.google.com/view"},
    ]

    link = _svc().upload_sync(
        b"scan-bytes",
        filename="upd.pdf",
        media_type="application/pdf",
        uploaded_on=date(2026, 6, 5),
    )
    assert link == "https://drive.google.com/view"

    create_calls = mock_files.create.call_args_list
    # 1st create = the dd.mm.yyyy folder under the parent.
    assert create_calls[0].kwargs["body"]["name"] == "05.06.2026"
    assert create_calls[0].kwargs["body"]["parents"] == ["parent-1"]
    assert create_calls[0].kwargs["body"]["mimeType"].endswith("folder")
    # 2nd create = the scan inside the day folder.
    assert create_calls[1].kwargs["body"]["name"] == "upd.pdf"
    assert create_calls[1].kwargs["body"]["parents"] == ["folder-1"]


def test_upload_reuses_existing_day_folder(mock_files):
    mock_files.list.return_value.execute.return_value = {
        "files": [{"id": "folder-x", "name": "05.06.2026"}]
    }
    mock_files.create.return_value.execute.return_value = {
        "id": "file-1",
        "webViewLink": "https://drive.google.com/v2",
    }

    link = _svc().upload_sync(
        b"d", filename="a.png", media_type="image/png", uploaded_on=date(2026, 6, 5)
    )
    assert link == "https://drive.google.com/v2"
    # Folder reused → only the FILE is created, not the folder.
    assert mock_files.create.call_count == 1
    assert mock_files.create.call_args.kwargs["body"]["parents"] == ["folder-x"]


def test_day_folder_lookup_is_cached_across_uploads(mock_files):
    mock_files.list.return_value.execute.return_value = {"files": [{"id": "folder-x"}]}
    mock_files.create.return_value.execute.return_value = {
        "id": "f",
        "webViewLink": "u",
    }
    svc = _svc()
    svc.upload_sync(b"d", filename="a.png", media_type="image/png", uploaded_on=date(2026, 6, 5))
    svc.upload_sync(b"d", filename="b.png", media_type="image/png", uploaded_on=date(2026, 6, 5))
    # Same day → folder looked up only once; two files created.
    assert mock_files.list.call_count == 1
    assert mock_files.create.call_count == 2


def test_upload_translates_http_error(mock_files):
    mock_files.list.return_value.execute.return_value = {"files": [{"id": "folder-x"}]}
    resp = MagicMock()
    resp.status = 403
    resp.reason = "Forbidden"
    mock_files.create.return_value.execute.side_effect = HttpError(
        resp, b'{"error": {"message": "storage quota exceeded"}}'
    )
    with pytest.raises(DriveUploadError):
        _svc().upload_sync(
            b"d",
            filename="a.png",
            media_type="image/png",
            uploaded_on=date(2026, 6, 5),
        )


def test_refresh_error_translated_to_drive_upload_error(mock_files):
    """An expired/revoked refresh token (``invalid_grant``) surfaces as a
    ``RefreshError`` on the first call (the folder lookup). It must become a
    ``DriveUploadError`` so the upload pipeline keeps it a SOFT failure and
    still writes the УПД row — a bad Drive token must never block the sheet.
    """
    mock_files.list.return_value.execute.side_effect = RefreshError(
        "invalid_grant: Bad Request", {"error": "invalid_grant"}
    )
    with pytest.raises(DriveUploadError):
        _svc().upload_sync(
            b"d",
            filename="a.png",
            media_type="image/png",
            uploaded_on=date(2026, 6, 5),
        )


def test_missing_oauth_config_raises():
    svc = DriveService(
        client_id="", client_secret="", refresh_token="", parent_folder_id="p"
    )
    with pytest.raises(DriveUploadError):
        svc.upload_sync(
            b"d",
            filename="a.png",
            media_type="image/png",
            uploaded_on=date(2026, 6, 5),
        )
