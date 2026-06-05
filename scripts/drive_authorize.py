"""One-time OAuth consent to obtain a Drive refresh token for scan archival.

The form archives scans as a *real user* (not the service account, which has no
Drive quota). This script runs the one-time consent on your machine and prints a
refresh token to paste into ``.env``.

Prerequisites (in Google Cloud Console, the SAME project as the service account):
  1. Enable the **Google Drive API**.
  2. APIs & Services → Credentials → Create credentials → **OAuth client ID** →
     application type **Desktop app** → download the JSON.
  3. OAuth consent screen: add your Google account as a **Test user** (so an
     unverified app can still authorize).

Run locally (a browser must be available on this machine):

    # point it at the downloaded OAuth client JSON:
    uv run python scripts/drive_authorize.py ~/Downloads/client_secret_xxx.json

    # ...or, if DRIVE_OAUTH_CLIENT_ID / DRIVE_OAUTH_CLIENT_SECRET are in .env:
    uv run python scripts/drive_authorize.py

A browser opens, you grant access to YOUR Google account, and the script prints
the three values to put in ``.env``:

    DRIVE_OAUTH_CLIENT_ID=...
    DRIVE_OAUTH_CLIENT_SECRET=...
    DRIVE_OAUTH_REFRESH_TOKEN=...

Then set DRIVE_FOLDER_ID=<folder id> and DRIVE_ENABLED=true.
"""

from __future__ import annotations

import json
import os
import sys

from google_auth_oauthlib.flow import InstalledAppFlow

# Full Drive scope: the upload targets a folder you already created (see
# app/services/drive.py for why the narrower drive.file scope isn't enough).
SCOPES = ["https://www.googleapis.com/auth/drive"]
_AUTH_URI = "https://accounts.google.com/o/oauth2/auth"
_TOKEN_URI = "https://oauth2.googleapis.com/token"


def _load_dotenv() -> None:
    """Best-effort: pull DRIVE_OAUTH_* from .env into os.environ."""
    try:
        from dotenv import load_dotenv  # provided transitively by pydantic-settings
    except Exception:
        return
    load_dotenv()


def _build_flow() -> tuple[InstalledAppFlow, str, str]:
    """Return (flow, client_id, client_secret) from a file arg or env."""
    if len(sys.argv) > 1:
        path = sys.argv[1]
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        section = data.get("installed") or data.get("web") or {}
        client_id = section.get("client_id", "")
        client_secret = section.get("client_secret", "")
        flow = InstalledAppFlow.from_client_secrets_file(path, scopes=SCOPES)
        return flow, client_id, client_secret

    _load_dotenv()
    client_id = os.environ.get("DRIVE_OAUTH_CLIENT_ID", "").strip()
    client_secret = os.environ.get("DRIVE_OAUTH_CLIENT_SECRET", "").strip()
    if not (client_id and client_secret):
        sys.exit(
            "No OAuth client provided. Pass the downloaded client_secret.json as "
            "an argument, or set DRIVE_OAUTH_CLIENT_ID / DRIVE_OAUTH_CLIENT_SECRET "
            "in .env first.\n"
            "  uv run python scripts/drive_authorize.py path/to/client_secret.json"
        )
    client_config = {
        "installed": {
            "client_id": client_id,
            "client_secret": client_secret,
            "auth_uri": _AUTH_URI,
            "token_uri": _TOKEN_URI,
            "redirect_uris": ["http://localhost"],
        }
    }
    flow = InstalledAppFlow.from_client_config(client_config, scopes=SCOPES)
    return flow, client_id, client_secret


def main() -> None:
    flow, client_id, client_secret = _build_flow()
    # access_type=offline + prompt=consent guarantees a refresh_token is issued.
    creds = flow.run_local_server(
        port=0, access_type="offline", prompt="consent", open_browser=True
    )
    if not creds.refresh_token:
        sys.exit(
            "No refresh token returned. Re-run; make sure you used "
            "prompt=consent (this script does) and a Desktop-app OAuth client."
        )

    print("\n" + "=" * 64)
    print("OK — paste these into .env:\n")
    print(f"DRIVE_OAUTH_CLIENT_ID={client_id}")
    print(f"DRIVE_OAUTH_CLIENT_SECRET={client_secret}")
    print(f"DRIVE_OAUTH_REFRESH_TOKEN={creds.refresh_token}")
    print("\nthen set DRIVE_FOLDER_ID=<folder id> and DRIVE_ENABLED=true")
    print("=" * 64)


if __name__ == "__main__":
    main()
