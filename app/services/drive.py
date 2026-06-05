"""Google Drive archival (SDK boundary: google-api-python-client).

This module is the ONLY place that imports ``googleapiclient``. It uploads the
original foreman scan into a date-named subfolder of a parent folder and returns
the file's ``webViewLink`` so the УПД row in the sheet can link straight to the
scan for manual review.

**Auth: OAuth user, not the service account.** A service account has no Drive
storage quota of its own, so any file it *creates* fails with ``storageQuota
Exceeded`` — sharing a My Drive folder with it does NOT help, because the new
file is owned by the SA, not the folder owner (confirmed 2026-06-05, same as the
2026-05-29 incident). So Drive archival uploads as a **real user** via an OAuth
refresh token (client id/secret + refresh token from ``scripts/drive_authorize.py``):
files land in that user's Drive, on that user's quota. The service account stays
in use for Sheets (it only *edits* a user-owned sheet — no new file, no quota).
SDK errors are translated into :class:`DriveUploadError`.
"""

from __future__ import annotations

import asyncio
from datetime import date as Date

from google.oauth2.credentials import Credentials as UserCredentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaInMemoryUpload

from app.core.errors import DriveUploadError
from app.core.logging import get_logger

log = get_logger("drive")

# Full Drive scope: the app writes the scan into a folder the *user* already
# created (``drive_folder_id``). The narrower ``drive.file`` scope only grants
# access to files the app itself created, so it can't target a pre-existing
# user folder — hence full ``drive`` here. It's the user's own account, an
# internal tool, with a one-time explicit consent.
_SCOPES = ("https://www.googleapis.com/auth/drive",)
_TOKEN_URI = "https://oauth2.googleapis.com/token"
_FOLDER_MIME = "application/vnd.google-apps.folder"


class DriveService:
    """Uploads original scans into ``<parent>/<dd.mm.yyyy>/`` and returns links.

    The Drive client and the per-day subfolder ids are cached on the instance so
    a batch of uploads on the same day pays the folder lookup only once. The
    handle is opened lazily on the first call so the singleton can be built at
    startup without a network round-trip.
    """

    def __init__(
        self,
        *,
        client_id: str,
        client_secret: str,
        refresh_token: str,
        parent_folder_id: str,
    ) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._refresh_token = refresh_token
        self._parent_folder_id = parent_folder_id
        self._service: object | None = None
        self._folder_cache: dict[str, str] = {}

    # --- public API ---------------------------------------------------------

    async def upload(
        self,
        data: bytes,
        *,
        filename: str,
        media_type: str,
        uploaded_on: Date,
    ) -> str:
        """Async wrapper over :meth:`upload_sync`."""
        return await asyncio.to_thread(
            self.upload_sync,
            data,
            filename=filename,
            media_type=media_type,
            uploaded_on=uploaded_on,
        )

    def upload_sync(
        self,
        data: bytes,
        *,
        filename: str,
        media_type: str,
        uploaded_on: Date,
    ) -> str:
        """Upload one file into the day's subfolder, return its ``webViewLink``.

        Blocks on network I/O. Translates SDK errors into
        :class:`DriveUploadError`.
        """
        service = self._get_service()
        folder_id = self._ensure_day_folder(service, uploaded_on)
        media = MediaInMemoryUpload(
            data,
            mimetype=media_type or "application/octet-stream",
            resumable=False,
        )
        body = {"name": filename, "parents": [folder_id]}
        try:
            created = (
                service.files()  # type: ignore[attr-defined]
                .create(
                    body=body,
                    media_body=media,
                    fields="id, webViewLink",
                    supportsAllDrives=True,
                )
                .execute()
            )
        except HttpError as exc:
            log.warning("drive.upload.api_error", error=str(exc), filename=filename)
            raise DriveUploadError(f"Google Drive API error: {exc}") from exc
        except Exception as exc:  # pragma: no cover - defensive
            log.exception("drive.upload.unexpected")
            raise DriveUploadError(
                f"Unexpected error uploading to Drive: {exc}"
            ) from exc

        link = created.get("webViewLink") or ""
        log.info(
            "drive.upload.ok",
            filename=filename,
            file_id=created.get("id"),
            folder_id=folder_id,
        )
        return link

    # --- internal -----------------------------------------------------------

    def _get_service(self) -> object:
        """Build + cache the Drive v3 client from the OAuth refresh token."""
        if self._service is not None:
            return self._service
        if not (self._refresh_token and self._client_id and self._client_secret):
            raise DriveUploadError(
                "Drive OAuth is not configured — set DRIVE_OAUTH_CLIENT_ID / "
                "DRIVE_OAUTH_CLIENT_SECRET / DRIVE_OAUTH_REFRESH_TOKEN "
                "(run scripts/drive_authorize.py to obtain the refresh token)"
            )
        try:
            # token=None → google-auth fetches a fresh access token from the
            # refresh token on the first request and refreshes as needed.
            creds = UserCredentials(
                None,
                refresh_token=self._refresh_token,
                token_uri=_TOKEN_URI,
                client_id=self._client_id,
                client_secret=self._client_secret,
                scopes=list(_SCOPES),
            )
            self._service = build(
                "drive", "v3", credentials=creds, cache_discovery=False
            )
        except Exception as exc:
            log.exception("drive.auth.failed")
            raise DriveUploadError(f"Failed to init Drive client: {exc}") from exc
        return self._service

    def _ensure_day_folder(self, service: object, uploaded_on: Date) -> str:
        """Find-or-create ``<parent>/<dd.mm.yyyy>`` and return its id (cached)."""
        name = uploaded_on.strftime("%d.%m.%Y")
        cached = self._folder_cache.get(name)
        if cached is not None:
            return cached
        folder_id = self._find_folder(service, name) or self._create_folder(
            service, name
        )
        self._folder_cache[name] = folder_id
        return folder_id

    def _find_folder(self, service: object, name: str) -> str | None:
        safe = name.replace("\\", "\\\\").replace("'", "\\'")
        query = (
            f"name = '{safe}' and mimeType = '{_FOLDER_MIME}' "
            f"and '{self._parent_folder_id}' in parents and trashed = false"
        )
        try:
            resp = (
                service.files()  # type: ignore[attr-defined]
                .list(
                    q=query,
                    spaces="drive",
                    fields="files(id, name)",
                    supportsAllDrives=True,
                    includeItemsFromAllDrives=True,
                )
                .execute()
            )
        except HttpError as exc:
            log.warning("drive.folder.list_api_error", error=str(exc), name=name)
            raise DriveUploadError(f"Google Drive API error: {exc}") from exc
        files = resp.get("files", [])
        return files[0]["id"] if files else None

    def _create_folder(self, service: object, name: str) -> str:
        body = {
            "name": name,
            "mimeType": _FOLDER_MIME,
            "parents": [self._parent_folder_id],
        }
        try:
            created = (
                service.files()  # type: ignore[attr-defined]
                .create(body=body, fields="id", supportsAllDrives=True)
                .execute()
            )
        except HttpError as exc:
            log.warning("drive.folder.create_api_error", error=str(exc), name=name)
            raise DriveUploadError(f"Google Drive API error: {exc}") from exc
        log.info("drive.folder.created", name=name, folder_id=created["id"])
        return created["id"]
