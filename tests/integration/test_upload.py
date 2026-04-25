"""Integration tests for POST /api/upload.

We skip the FastAPI lifespan (it touches structlog globals and cached clients)
and drive the ASGI app directly through ``httpx.ASGITransport`` with
dependency overrides for the three services.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import date
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from app.core.errors import SheetsAppendError, VisionExtractionError
from app.deps import get_files_service, get_sheets_service, get_vision_service
from app.main import app as real_app
from app.models import DownloadedFile, UPDRecord


@asynccontextmanager
async def _noop_lifespan(_):
    yield


@pytest.fixture
def app():
    """App with lifespan disabled — tests don't want structlog reconfig."""
    real_app.router.lifespan_context = _noop_lifespan  # type: ignore[attr-defined]
    yield real_app
    real_app.dependency_overrides.clear()


@pytest.fixture
def fake_files():
    svc = MagicMock()
    svc.to_png = AsyncMock(
        return_value=DownloadedFile(
            data=b"\x89PNG-fake",
            media_type="image/png",
            original_name="upd.png",
            page_count=None,
        )
    )
    return svc


@pytest.fixture
def fake_vision():
    svc = MagicMock()
    svc.extract = AsyncMock(
        return_value=UPDRecord(
            organization='ООО "Озеленский"',
            date=date(2026, 4, 22),
            amount=10500.0,
            upd_number="6022635717",
        )
    )
    return svc


@pytest.fixture
def fake_sheets():
    svc = MagicMock()
    svc.append_row = AsyncMock(return_value=None)
    return svc


@pytest.fixture
def client(app, fake_files, fake_vision, fake_sheets):
    app.dependency_overrides[get_files_service] = lambda: fake_files
    app.dependency_overrides[get_vision_service] = lambda: fake_vision
    app.dependency_overrides[get_sheets_service] = lambda: fake_sheets
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


def _png_payload(name: str = "upd.png") -> dict:
    return {"file": (name, b"\x89PNG\r\n\x1a\nfake-image-bytes", "image/png")}


def _pdf_payload(name: str = "upd.pdf") -> dict:
    return {"file": (name, b"%PDF-1.4\nfake", "application/pdf")}


@pytest.mark.asyncio
async def test_happy_path_image(client, fake_files, fake_vision, fake_sheets):
    async with client as c:
        resp = await c.post(
            "/api/upload",
            files=_png_payload(),
            data={"foreman": "Юра"},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["needs_review"] is False
    assert body["sheet_url"].startswith("https://docs.google.com/spreadsheets/")
    assert body["record"]["organization"] == 'ООО "Озеленский"'

    fake_files.to_png.assert_awaited_once()
    fake_vision.extract.assert_awaited_once()
    fake_sheets.append_row.assert_awaited_once()
    call = fake_sheets.append_row.await_args
    assert call.kwargs["foreman"] == "Юра"
    assert call.kwargs["correlation_id"] == body["correlation_id"]


@pytest.mark.asyncio
async def test_happy_path_pdf(client, fake_sheets):
    async with client as c:
        resp = await c.post(
            "/api/upload",
            files=_pdf_payload(),
            data={"foreman": "Гриша"},
        )
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    fake_sheets.append_row.assert_awaited_once()


@pytest.mark.asyncio
async def test_needs_review_skips_sheets(client, fake_vision, fake_sheets):
    fake_vision.extract = AsyncMock(
        return_value=UPDRecord(
            organization='ООО "Test"',
            date=date(2026, 4, 22),
            amount=None,  # missing → needs_review
            upd_number="123",
        )
    )
    async with client as c:
        resp = await c.post(
            "/api/upload",
            files=_png_payload(),
            data={"foreman": "Боря"},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["needs_review"] is True
    assert "amount" in body["missing_fields"]
    assert body["sheet_url"] is None
    fake_sheets.append_row.assert_not_awaited()


@pytest.mark.asyncio
async def test_invalid_foreman(client):
    async with client as c:
        resp = await c.post(
            "/api/upload",
            files=_png_payload(),
            data={"foreman": "Вася"},
        )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_missing_file(client):
    async with client as c:
        resp = await c.post("/api/upload", data={"foreman": "Юра"})
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_unsupported_content_type(client):
    async with client as c:
        resp = await c.post(
            "/api/upload",
            files={"file": ("note.txt", b"hello", "text/plain")},
            data={"foreman": "Юра"},
        )
    assert resp.status_code == 415


@pytest.mark.asyncio
async def test_empty_file(client):
    async with client as c:
        resp = await c.post(
            "/api/upload",
            files={"file": ("empty.png", b"", "image/png")},
            data={"foreman": "Юра"},
        )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_too_large_file(client, monkeypatch):
    from app.config import get_settings as _gs

    settings = _gs()
    monkeypatch.setattr(settings, "max_upload_bytes", 16, raising=False)

    async with client as c:
        resp = await c.post(
            "/api/upload",
            files={"file": ("upd.png", b"x" * 64, "image/png")},
            data={"foreman": "Юра"},
        )
    assert resp.status_code == 413


@pytest.mark.asyncio
async def test_vision_failure(client, fake_vision, fake_sheets):
    fake_vision.extract = AsyncMock(side_effect=VisionExtractionError("boom"))
    async with client as c:
        resp = await c.post(
            "/api/upload",
            files=_png_payload(),
            data={"foreman": "Юра"},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    assert body["error"] == "vision_extraction_error"
    fake_sheets.append_row.assert_not_awaited()


@pytest.mark.asyncio
async def test_sheets_failure(client, fake_sheets):
    fake_sheets.append_row = AsyncMock(side_effect=SheetsAppendError("api 503"))
    async with client as c:
        resp = await c.post(
            "/api/upload",
            files=_png_payload(),
            data={"foreman": "Юра"},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    assert body["error"] == "sheets_append_error"
