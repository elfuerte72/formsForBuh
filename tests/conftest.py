"""Shared test fixtures."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

# Ensure required settings are present before anything imports app.config.
os.environ.setdefault("ANTHROPIC_API_KEY", "test-anthropic-key")
os.environ.setdefault("WEBHOOK_SECRET", "test-webhook-secret")
os.environ.setdefault("LOG_LEVEL", "DEBUG")
os.environ.setdefault("LOG_FORMAT", "pretty")

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
