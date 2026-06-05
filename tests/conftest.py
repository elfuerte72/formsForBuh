"""Shared test fixtures."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

# Ensure required settings are present before anything imports app.config.
os.environ.setdefault("ANTHROPIC_API_KEY", "test-anthropic-key")
os.environ.setdefault("SHEET_ID", "test-sheet-id")
os.environ.setdefault(
    "GOOGLE_CREDENTIALS_JSON",
    (
        '{"type":"service_account","project_id":"test-project",'
        '"private_key_id":"abc","private_key":"-----BEGIN PRIVATE KEY-----\\n'
        'fake\\n-----END PRIVATE KEY-----\\n",'
        '"client_email":"test@test.iam.gserviceaccount.com",'
        '"client_id":"1","token_uri":"https://oauth2.googleapis.com/token"}'
    ),
)
os.environ.setdefault("LOG_LEVEL", "DEBUG")
os.environ.setdefault("LOG_FORMAT", "pretty")
# Pin feature flags so tests never inherit a developer's real .env (e.g. a live
# DRIVE_ENABLED=true). Tests that need a flag on flip it explicitly.
os.environ.setdefault("RECONCILIATION_ENABLED", "false")
os.environ.setdefault("DRIVE_ENABLED", "false")

from app.config import get_settings  # noqa: E402


FIXTURES_DIR = Path(__file__).parent / "fixtures" / "upd"


@pytest.fixture(scope="session")
def upd_image_bytes() -> bytes:
    """Real UPD screenshot used as a regression sample (sample #1)."""
    path = FIXTURES_DIR / "upd_sample_1.png"
    return path.read_bytes()


@pytest.fixture(scope="session")
def upd_image_path() -> Path:
    return FIXTURES_DIR / "upd_sample_1.png"


@pytest.fixture(autouse=True)
def _clear_settings_cache():
    """Reset cached settings between tests so env overrides take effect."""
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()
